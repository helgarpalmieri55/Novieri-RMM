"""Core kernel entities per docs/04-data-model.md.

Every tenant-scoped table carries org_id and is covered by an RLS policy
(migration 0002). Module tables live in their own modules, never here.
"""

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    FetchedValue,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, uuid7_pk


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = uuid7_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("org_id", "external_subject"),
        # Migration 0032. Declared here as well as in the migration so
        # `alembic revision --autogenerate` is not one run away from proposing
        # a DROP, and so `test_schema_drift.py` can hold the two in agreement.
        # `email_intake/identity.py::resolve_sender` matches on `lower(email)`
        # and needs this to be an index scan.
        Index("ix_users_org_lower_email", "org_id", text("lower(email)")),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    external_subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    # Migration 0066. The three fields ITMS owns about a person, as against the
    # ones Keycloak owns (spec §7.1: password, MFA, passkeys, email). All
    # nullable with no default -- NULL is "not stated", which is not the same
    # fact as a cleared field, and there is no defensible non-null timezone for
    # somebody the platform has never heard from.
    #
    # `phone` is DIRECTORY DATA, not a second factor (§7.4 falsifier 15):
    # nothing here sends SMS, and a column its own subject may write must never
    # become something that authenticates them.
    phone: Mapped[str | None] = mapped_column(String(40), default=None)
    timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    locale: Mapped[str | None] = mapped_column(String(16), default=None)
    # Spec §1.3. `staff` is a Novieri technician (console); `contact` is a
    # person of one client (portal only, refused by `auth_context`). The CHECK
    # `ck_users_kind_client` (migration 0230) ties the two columns together.
    kind: Mapped[str] = mapped_column(String(10), default="staff", server_default="staff")
    # Indexed by migration 0231, which also gives it the client policy (O, with
    # its own WITH CHECK) and the org-consistency key `fk_users_client_org`.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID | None] = mapped_column(ForeignKey("organizations.id"), default=None)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[UUID] = uuid7_pk()
    code: Mapped[str] = mapped_column(String(100), unique=True)
    module: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(50))


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[UUID] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class UserRole(Base):
    """One grant of one role to one user, for every client or for one client (spec §2.1).

    IT TAKES BOTH ENTRIES BELOW TO SAY "ONCE", and the constraint alone said it
    only for the half that matters least. `UNIQUE (user_id, role_id,
    device_group_id)` is a three-column unique over a NULLABLE column, and in
    Postgres two NULLs are never equal -- so the constraint indexes an
    org-wide grant but can never find a duplicate of one. Every org-wide grant
    carries `device_group_id IS NULL`, which made the rule inert for exactly
    the grants the console shows as "organization-wide": the same `INSERT`
    could run without limit, and did (three identical `super-admin` rows on the
    operator's deployment).

    The partial unique index (migration 0073) covers that half: `(user_id,
    role_id) WHERE device_group_id IS NULL`, where the predicate does the
    NULL-matching the constraint cannot. The constraint stays because the
    group-scoped half is genuinely correct -- no NULL, so ordinary equality
    holds -- and the two together allow exactly one org-wide grant plus at most
    one grant per group.

    `org_id` IS DELIBERATELY NOT IN THE INDEX. `users.org_id` is NOT NULL and a
    user belongs to one organization, so `user_id` already determines the
    tenant; both `UserRole(` constructors in `app/` write the target user's own
    org. Adding `org_id` would only widen the key -- one duplicate per distinct
    `org_id` value written against the same user -- so it would weaken the rule
    rather than scope it. Uniqueness is enforced by the index itself, below
    RLS, so tenancy needs nothing from the key here.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "client_id"),
        Index(
            "uq_user_roles_user_role_org_wide",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=text("client_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"))
    # Spec §2.1: NULL is "every client, including ones created later".
    client_id: Mapped[UUID | None] = mapped_column(ForeignKey("clients.id"), default=None)


class DeviceGroup(TimestampMixin, Base):
    """A named, addressable subset of one org's fleet.

    UNIQUE ON `(org_id, name)` ONLY WHERE LIVE (migration 0072). `archived_at`
    is a soft delete -- the hash-chained audit log carries group ids forever
    and `deployments.target_id` names one by bare uuid with no FK, so a hard
    row deletion either raises on a `NO ACTION` FK or dangles silently. A soft
    delete under a TOTAL unique constraint would let an archived group hold
    its name against every future group in the org, so the constraint is
    partial instead.
    """

    __tablename__ = "device_groups"
    __table_args__ = (
        Index(
            "uq_device_groups_org_live_name",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_device_groups_org_precedence_name", "org_id", "precedence", "name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0231).
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    parent_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    description: Mapped[str] = mapped_column(Text, default="")
    #: Membership rules, or SQL NULL for a hand-curated group.
    #:
    #: `none_as_null=True` IS NOT A STYLE CHOICE, IT IS THE DIFFERENCE BETWEEN
    #: NULL AND `'null'`. SQLAlchemy's JSON types serialize a Python `None`
    #: into the JSON literal `null` by default, so every static group created
    #: through `POST /device-groups` was written with `rules = 'null'::jsonb`
    #: -- a value for which `rules IS NOT NULL` is TRUE. Deserialization hides
    #: it perfectly: the value comes back as Python `None`, so `dynamic`
    #: (`group.rules is not None`) has always read correctly and no response
    #: was ever wrong.
    #:
    #: What was wrong is every SQL predicate. `evaluate_rules` selected each
    #: static group as rule-bearing and then failed to run its empty document,
    #: which it handles by logging a warning and skipping -- so an ingest on a
    #: fleet with twenty static groups emitted twenty warnings and worked, and
    #: the "`logger.warning` nobody reads" §5.1 warns about was being written
    #: for real. The re-evaluation sweep is where it stops being cosmetic: its
    #: short circuit asks whether the org holds any rule-bearing group, and
    #: with `'null'` the answer was always yes, so an org using only static
    #: groups would have read its entire fleet's inventory documents every five
    #: minutes to decide nothing.
    #:
    #: Rows already written as `'null'` are handled by
    #: `inventory.rules.rule_bearing_groups_query`, which asks
    #: `jsonb_typeof(rules) = 'object'` rather than `IS NOT NULL`. Hence no
    #: migration: this changes what new writes store, that changes what every
    #: read believes, and neither is DDL.
    rules: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    #: WHICH RULE WINS, and it is a number rather than the alphabet on purpose.
    #: `inventory.rules.evaluate_rules` orders rule-bearing groups
    #: `(precedence, name)` and the first match claims the device; before
    #: migration 0072 it ordered on `name` alone, so a rename re-segmented a
    #: fleet. Lower sorts first. Name is now a label.
    precedence: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0")
    )
    #: Soft delete. Set by `DELETE /api/v1/device-groups/{id}`, which refuses
    #: while the group holds any live reference, so an archived group is empty
    #: and unreferenced by construction.
    archived_at: Mapped[datetime | None] = mapped_column(default=None)


class DeviceGroupEvaluation(Base):
    """What the membership re-evaluation sweep did to ONE org, last time it ran.

    ONE ROW PER ORG, OVERWRITTEN IN PLACE. This is not a history table and must
    not become one: the history is the hash-chained audit log's
    `device_group.membership_reevaluated` events, which are append-only and
    already carry the per-run counts. This row answers the two questions that
    log cannot be asked from the Groups page.

    **WHY IT EXISTS AT ALL — QUESTION 1: "IS THE SWEEP RUNNING?"**
    `docs/superpowers/specs/2026-08-22-device-group-management-design.md` §5.3
    and §9 say the group page "states when membership was last re-evaluated …
    a timestamp, never alone", and cycle 2 could not deliver it: the sweep's
    only durable record was that audit event, reading it needs `audit.read`,
    and the Groups page is gated `devices.read`. So the page stated the
    INTERVAL instead — "up to five minutes behind" — which cannot distinguish
    *swept four minutes ago* from *the worker has been down since Tuesday*.
    `last_evaluated_at` is written on EVERY run, including the short-circuit
    run for an org that holds no rule-bearing group at all, because "the worker
    is alive" is exactly the fact an org with no rules still needs.

    **QUESTION 2: "WHICH DEVICES STILL NEED LOOKING AT?"** The sweep re-read
    every active device's latest `InventoryDocument` every 300 seconds — a
    machine's whole collected estate, tens of kilobytes each, so a
    10,000-device org moved a few hundred megabytes per sweep forever.
    `SNAPSHOT_BATCH` bounds the resident set, not the transfer. The watermark
    columns below are what make the pass incremental.

    **THE WATERMARK IS TWO COLUMNS AND THAT IS DELIBERATE.** A single
    "max snapshot id at this run's start" has a commit-lag race: a snapshot
    whose uuid7 was assigned before the sweep read `max(id)` but whose
    transaction committed after would sit forever below the boundary and never
    be evaluated — the device silently keeps a membership its inventory no
    longer justifies, with nothing on any screen saying so. So the boundary in
    use lags one run behind: `next_watermark_snapshot_id` records this run's
    maximum, and the NEXT run promotes it into `watermark_snapshot_id`. Every
    snapshot is therefore offered to the evaluator across a full
    `REEVALUATE_INTERVAL_S` window rather than at one instant. Re-evaluating a
    device twice is free — a device already in the group its rule claims is not
    moved again — and no clock is involved, which matters because
    `inventory_snapshots.taken_at` is the AGENT's time and a skewed endpoint
    would move the boundary for the whole org.
    """

    __tablename__ = "device_group_evaluation"

    #: THE PRIMARY KEY, so "one row per org" is a schema property rather than a
    #: convention the service is trusted to keep.
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    #: When the sweep last COMPLETED for this org. Written on every run.
    last_evaluated_at: Mapped[datetime] = mapped_column()
    #: When it last read EVERY active device rather than only the changed ones.
    #: The console needs both: an incremental pass proves the worker is alive,
    #: a full pass is what bounds every drift the watermark cannot see.
    last_full_pass_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The boundary in use: only devices with a snapshot newer than this are
    #: re-evaluated. NULL forces a full pass, which is the first run's state.
    watermark_snapshot_id: Mapped[UUID | None] = mapped_column(default=None)
    #: The maximum `inventory_snapshots.id` seen at the START of the last run.
    #: Promoted into `watermark_snapshot_id` by the NEXT run -- see the class
    #: docstring for why the boundary lags one interval.
    next_watermark_snapshot_id: Mapped[UUID | None] = mapped_column(default=None)
    #: sha256 over this org's rule-bearing groups: their ids, precedence and
    #: rules documents. A change means a rule now claims a set nobody has
    #: evaluated, so the next pass is full whatever the watermark says. Rules
    #: are what decide where a signed job lands; this is the one input whose
    #: change must never wait for an ingest.
    rules_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    #: The last run's counts, as the console renders them beside the timestamp.
    groups_evaluated: Mapped[int] = mapped_column(default=0)
    devices_evaluated: Mapped[int] = mapped_column(default=0)
    devices_moved: Mapped[int] = mapped_column(default=0)
    devices_without_inventory: Mapped[int] = mapped_column(default=0)
    #: Inventory documents actually read off the wire. THIS is the number the
    #: watermark exists to move, so it is recorded rather than inferred: a
    #: regression that quietly restores the full read shows up here as a count
    #: that stops falling.
    snapshots_read: Mapped[int] = mapped_column(default=0)
    #: Whether the last run read every active device. False is the steady state.
    full_pass: Mapped[bool] = mapped_column(Boolean, default=True)


class Device(TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (
        Index("ix_devices_org_hostname", "org_id", "hostname"),
        # 0078 CREATED THIS AND DID NOT DECLARE IT, which is the same shape of
        # accident that deleted `ix_doc_articles_fts` for a month: an index the
        # model metadata has never heard of comes back from the next
        # `alembic revision --autogenerate` as a bare `op.drop_index`, one line
        # of apparent noise in a long diff. This one was four days old and one
        # autogenerate away from gone. `test_migrations.py` now compares both
        # sets on every run, so the class is caught rather than remembered.
        Index("ix_devices_org_provenance", "org_id", "provenance"),
        # Migration 0170. Declared here for the reason the index above gives:
        # the drift test compares the model with the live schema.
        Index("ix_devices_org_review_state", "org_id", "review_state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0231).
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    # Spec §1.5: a site of the same client (devices: trigger
    # its_device_client_guard).
    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id"))
    # NULLABLE SINCE 0061, and the two are the same decision. A switch has a
    # name but no hostname in this sense and no OS family at all; a laptop in a
    # box has told us neither yet. Both were NOT NULL, which is exactly what
    # kept them out of the inventory (asset-inventory spec §1).
    hostname: Mapped[str | None] = mapped_column(String(255), default=None)
    fqdn: Mapped[str | None] = mapped_column(String(255), default=None)
    os_family: Mapped[str | None] = mapped_column(  # windows | linux | macos
        String(20), default=None
    )
    os_version: Mapped[str] = mapped_column(String(100), default="")
    architecture: Mapped[str] = mapped_column(String(20), default="")
    serial: Mapped[str | None] = mapped_column(String(100), default=None)
    asset_tag: Mapped[str | None] = mapped_column(String(100), default=None)
    cpu: Mapped[str] = mapped_column(String(200), default="")
    ram_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    disks: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    nics: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    first_seen_at: Mapped[datetime | None] = mapped_column(default=None)
    last_seen_at: Mapped[datetime | None] = mapped_column(default=None)

    #: WHAT THE OPERATOR INTENDS THIS ASSET TO BE -- stored, because nothing can
    #: derive it. `in_service | stock | unmanageable | retired`.
    #:
    #: NOT whether the record has an agent, which is a JOIN (`agents.device_id`
    #: is a unique FK to `devices.id`) and is never stored. The distinction is
    #: the point: "unmanaged" means no `agents` row and belongs on a to-do list,
    #: while "unmanageable" means the operator said a switch will never have
    #: one, and must never appear on that list. A single `management_state`
    #: column would conflate two facts that change independently, and the
    #: disagreement would be silent (asset-inventory spec §2).
    lifecycle: Mapped[str] = mapped_column(String(20), default="in_service")
    asset_type: Mapped[str] = mapped_column(String(20), default="computer")
    #: How this record came to exist -- `agent | manual | discovery` -- which is
    #: the first question an operator asks when a row looks wrong.
    source: Mapped[str] = mapped_column(String(12), default="agent")
    #: WHO, IF ANYONE, STOOD BEHIND THIS ROW: `agent | operator | promoted |
    #: sweep`.
    #:
    #: Since the operator's ruling of 2026-08-26 a `net.sweep` records every
    #: host it finds, with nobody selecting anything -- so the inventory now
    #: contains rows built from an echo reply beside rows a person entered. This
    #: column is what keeps them tellable apart: `sweep` means the platform
    #: wrote it on its own evidence and no human has confirmed it exists.
    #:
    #: NOT A SECOND `source`. `source` names the MECHANISM; this names the
    #: WARRANT, and they differ exactly once -- a `discovery` row may be
    #: `promoted` (a person picked it off the Discovered screen) or `sweep` (the
    #: sweep recorded it). Migration 0078's CHECK constraint makes every other
    #: pairing unrepresentable, so the two cannot drift into disagreeing.
    #:
    #: IMMUTABLE AFTER CREATION, like `source`. An agent enrolling onto a swept
    #: row enriches it with reported facts but does not retroactively make a
    #: human have vouched for it; that the row is now managed is a join on
    #: `agents`, which is a different and already-answered question.
    provenance: Mapped[str] = mapped_column(String(12), default="agent")
    description: Mapped[str | None] = mapped_column(String(1000), default=None)
    primary_ip: Mapped[str | None] = mapped_column(INET, default=None)
    owner: Mapped[str | None] = mapped_column(String(200), default=None)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    #: `approved | pending | rejected` (migration 0170, install-link spec §1.5).
    #:
    #: `pending` is a machine that joined through the install link and that no
    #: person has looked at yet: `JobEngine.create` lets it receive inventory,
    #: health monitoring and certificate lifecycle jobs and refuses everything
    #: else (`app/jobs/review_gate.py`). `rejected` is the tombstone a Reject
    #: leaves (plan D1) -- hidden from every list, and `decommissioned` once the
    #: agent confirmed its uninstall or 24 hours passed.
    review_state: Mapped[str] = mapped_column(
        String(10), default="approved", server_default="approved"
    )
    #: The source address of the link enrollment that created this row, or NULL
    #: for any other way a row comes to exist.
    link_joined_from: Mapped[str | None] = mapped_column(INET, default=None)
    #: When that link enrollment happened. The hourly limit counts these.
    link_joined_at: Mapped[datetime | None] = mapped_column(default=None)
    #: When a technician rejected it. The completion sweep reads this.
    rejected_at: Mapped[datetime | None] = mapped_column(default=None)
    #: How the rejection ENDED (migration 0170): NULL while the agent still has
    #: time to confirm its uninstall; `agent_confirmed`; `timeout`;
    #: `agent_did_not_confirm` (the uninstall job failed, was refused or
    #: expired); `agent_cannot_uninstall` (too old to be told). The last three
    #: mean the agent may still be on the computer and someone has to remove
    #: it by hand.
    rejection_outcome: Mapped[str | None] = mapped_column(String(24), default=None)
    #: When a technician said they removed the agent by hand (migration 0170).
    #: Only a rejection whose outcome leaves the agent behind can carry it, and
    #: until it is set that row is listed in *Waiting for review*.
    removal_acknowledged_at: Mapped[datetime | None] = mapped_column(default=None)


class SuggestedAssetMatch(Base):
    """Two device rows that MIGHT be the same machine, for a human to decide.

    WHY THIS IS NOT A LINK. An enrolling agent auto-links only on a physical
    MAC. On weaker evidence -- a shared hostname, a shared address, or two
    records matching at once -- the platform refuses, because auto-linking on a
    hostname silently merges two real machines that share a name across sites,
    and that merge is not recoverable: nothing afterwards says which inventory
    data belonged to which. A suggestion queue is mildly annoying; a bad merge
    is data loss discovered months later (asset-inventory spec §4).

    `basis` records WHY it was a suggestion, which is the first thing the
    operator resolving it needs and is not derivable after the fact.

    Open pairs are unique (partial index on `resolved_at IS NULL`, migration
    0063): a machine that re-enrols must not stack forty copies of one
    question, because an operator facing that stops reading the queue.
    """

    __tablename__ = "suggested_asset_match"
    __table_args__ = (
        # Unique per OPEN pair only. A device that re-enrols -- a rebuild, a
        # certificate rotation that went the long way round -- must not stack a
        # second identical question, because an operator facing forty copies of
        # one stops reading the queue. Partial, so a pair rejected in March can
        # legitimately be raised again in June if the evidence recurs.
        Index(
            "uq_sam_open_pair",
            "device_id",
            "candidate_device_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_sam_open", "org_id", "resolved_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    #: The row enrolment just created.
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    #: The row that already existed and might be the same machine.
    candidate_device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    #: `hostname | ip | multiple`
    basis: Mapped[str] = mapped_column(String(20))
    #: No `updated_at`: a suggestion is not edited, it is RESOLVED, and
    #: `resolved_at` is the only state change it has.
    created_at: Mapped[datetime] = mapped_column(server_default="now()")
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    #: `linked | rejected`, or NULL while open.
    resolution: Mapped[str | None] = mapped_column(String(20), default=None)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )


class DeviceTag(Base):
    __tablename__ = "device_tags"
    __table_args__ = (UniqueConstraint("device_id", "key"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(String(500), default="")


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), unique=True)
    cert_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    cert_serial: Mapped[str] = mapped_column(String(64))
    cert_not_after: Mapped[datetime] = mapped_column()
    cert_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    enrolled_at: Mapped[datetime] = mapped_column()
    agent_version: Mapped[str] = mapped_column(String(50), default="")
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # When the gateway last recorded the agent's self-reported version header
    # (spec-self-update.md §3.3). Display and eligibility only.
    agent_version_reported_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(20), default="offline")  # online | offline
    last_seen_at: Mapped[datetime | None] = mapped_column(default=None)
    last_ip: Mapped[str | None] = mapped_column(String(64), default=None)


class Policy(TimestampMixin, Base):
    __tablename__ = "policies"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    device_group_id: Mapped[UUID | None] = mapped_column(  # NULL = org-wide scope
        ForeignKey("device_groups.id"), default=None
    )
    module: Mapped[str] = mapped_column(String(50))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)


class Credential(Base):
    """Credential METADATA only — the secret lives in the vault (docs/03-security-model.md)."""

    __tablename__ = "credentials"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_group_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))  # windows_admin | ssh_key | domain_join | ...
    device_group_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("device_groups.id"), default=None
    )
    vault_ref: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime | None] = mapped_column(server_default="now()")
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    # Envelope-encrypted secret (docs/kernel/vault.md). Null = metadata-only
    # placeholder; the plaintext is never stored in any column.
    ciphertext: Mapped[str | None] = mapped_column(Text, default=None)
    wrapped_dek: Mapped[str | None] = mapped_column(Text, default=None)
    key_version: Mapped[int | None] = mapped_column(default=None)


class JobStatus(enum.StrEnum):
    """The signed-job lifecycle
    (docs/superpowers/specs/2026-08-22-job-lifecycle-design.md §3.2).

    `cancelled` IS APPENDED, NOT INSERTED. The Postgres type's
    `enumsortorder` is what an `ORDER BY status` means, so a value placed
    among the others would re-sort rows written before it existed.
    """

    queued = "queued"
    dispatched = "dispatched"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    expired = "expired"
    refused = "refused"
    #: Terminal, like the four above it. Reached only through a deliberate
    #: human act -- never by a timer, a sweep or an agent report.
    cancelled = "cancelled"


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_org_status", "org_id", "status"),
        Index("ix_jobs_target", "target_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_job_client from the
    # device or group named by target_type/target_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    type: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(10), default="device")  # device | group
    target_id: Mapped[UUID] = mapped_column()
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    signature: Mapped[str | None] = mapped_column(Text, default=None)
    nonce: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.queued,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), default=None)
    # ---- cancellation (migration 0071) ----------------------------------
    # Three columns rather than a second status, because a job that is running
    # while a cancel is in flight IS STILL RUNNING; replacing that with a
    # `cancelling` badge would delete a true fact to display a pending one.
    #: When a human asked for this job to stop. NULL means nobody ever did,
    #: which is what `app/worker.py::sweep_org` reads to decide whether a
    #: silent `dispatched` job may be re-signed and republished.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(default=None)
    #: Who asked. DELIBERATELY NOT RETURNED BY ANY API ROUTE: the actor belongs
    #: in the hash-chained audit log, and `JobOut` discloses no user id today.
    cancel_requested_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    #: What actually became of the request: `requested`, `withdrawn`,
    #: `dropped`, `interrupted`, `too_late` or `unsupported` (design §3.2).
    #: `withdrawn` is the only one the server writes on its OWN authority --
    #: the job was never sent, so nothing on any endpoint can contradict it.
    #: Every other value is an endpoint's own answer, or the record that one is
    #: outstanding.
    cancel_outcome: Mapped[str | None] = mapped_column(String(24), default=None)
    # ---- retry lineage (migration 0076) ---------------------------------
    #: The job this one re-runs, or NULL. A retry is a NEW ROW -- new id, new
    #: nonce, a signature over different bytes, params re-validated and passed
    #: through the type's refresher -- and the original is never written to
    #: (design §4.1). This column is the only link between them.
    #:
    #: IT IS NOT A REPLAY HANDLE. Nothing re-executes the original's stored
    #: signed bytes; that would be a replay primitive, and the agent's nonce
    #: store would refuse it anyway. What this records is provenance, so the
    #: audit chain and the console can show two acts and their relationship.
    retry_of_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.id"), default=None
    )


class JobLog(Base):
    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_job_logs_job", "job_id"),)

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the jobs row named by job_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    ts: Mapped[datetime] = mapped_column()
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text)
    artifact_ref: Mapped[str | None] = mapped_column(String(500), default=None)


class AuditEvent(Base):
    """Append-only, hash-chained. No updated_at — rows are immutable (migration 0004)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_id_id", "org_id", "id"),
        # Migration 0016, for change_mgmt's per-target audit trail read
        # (`change_mgmt/reporting.py`). Declared here for the same reason as
        # `ix_users_org_lower_email` above.
        Index("ix_audit_target", "org_id", "target_type", "target_id"),
        # Migration 0220, for the console's AI-connections read
        # (`app/mcp/api.py::list_connections`): one org's tool calls in a window.
        Index(
            "ix_audit_mcp_tool_called",
            "org_id",
            "ts",
            postgresql_where=text("action = 'mcp.tool_called'"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    ts: Mapped[datetime] = mapped_column()
    actor_type: Mapped[str] = mapped_column(String(10))  # user | agent | system
    actor_id: Mapped[UUID | None] = mapped_column(default=None)
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50), default="")
    target_id: Mapped[str] = mapped_column(String(100), default="")
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0231).
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    # Spec §1.5: a site of the same client (devices: trigger
    # its_device_client_guard).
    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    expires_at: Mapped[datetime] = mapped_column()
    max_uses: Mapped[int | None] = mapped_column(default=None)
    use_count: Mapped[int] = mapped_column(default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime | None] = mapped_column(server_default="now()")


class InstallLink(Base):
    """One organisation's install link (install-link spec §1.1).

    THE CODE IS NOT HERE. `code_hash` is `sha256(code)` hex; the code itself is
    returned once, by the response that minted it, exactly as an enrollment
    token is. Unique across orgs because the public page and `/enroll/v1`
    resolve the organisation FROM the hash.
    """

    __tablename__ = "install_links"
    __table_args__ = (
        # One link per SITE (spec §1.5, migration 0231), no longer one per org.
        UniqueConstraint("site_id", name="uq_install_links_site"),
        UniqueConstraint("code_hash", name="uq_install_links_code_hash"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (C). Set by the application (migration 0231).
    client_id: Mapped[UUID] = mapped_column(ForeignKey("clients.id"), index=True)
    # Spec §1.5: a site of the same client (devices: trigger
    # its_device_client_guard).
    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    code_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    networks: Mapped[list[str]] = mapped_column(
        ARRAY(CIDR()), default=list, server_default=text("'{}'::cidr[]")
    )
    disable_at: Mapped[datetime | None] = mapped_column(default=None)
    hourly_limit: Mapped[int] = mapped_column(default=50, server_default=text("50"))
    #: When the link was last switched on; NULL while it is off. "How long it
    #: has been on" and "how many joined since" are both measured from here.
    enabled_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The last `enrollment.link_rate_limited` audit row, so a flood of 429s
    #: writes one row an hour rather than one per request.
    rate_limited_audit_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KeycloakEventIngest(Base):
    """One Keycloak event already forwarded into the audit chain.

    A DEDUPE LEDGER, NOT EVIDENCE. Keycloak's event filters are day-granular,
    so an hourly sweep re-reads the same day twenty-four times; the audit event
    is written only when an insert here takes. A duplicate would not corrupt
    the hash chain, it would pollute it -- an auditor counting passkey
    registrations would count each one once per sweep.

    `fingerprint` is unique GLOBALLY rather than per-org: one Keycloak event
    must be forwarded once even though the realm is shared by every tenant, and
    a per-org constraint would let it land twice if a user's org attribute
    changed between sweeps.
    """

    __tablename__ = "keycloak_event_ingest"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_keycloak_event_fingerprint"),
        Index("ix_keycloak_event_age", "ingested_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(server_default="now()")


class DomainEvent(Base):
    """Kernel transactional outbox (docs/modules/notification/spec.md §2.1, §3).

    Written in the SAME transaction as the state change it describes, so an
    event can never describe a fact that was rolled back, and a crash between
    "ticket assigned" and "notify the assignee" is not a reachable state.
    Drained by the Celery worker.

    Lives here rather than in app/modules/notification because Phase 16
    (endpoint health & alerting) publishes to this same facility -- putting it
    in the notification package would make alerting depend on notification for
    infrastructure reasons.
    """

    __tablename__ = "domain_events"
    __table_args__ = (
        # Whole-branch review finding M11: this index cannot serve the drain
        # query (`app.worker.drain_outbox` filters `org_id, processed_at IS
        # NULL` -- neither `claimed_at` nor `occurred_at` appears in that
        # predicate). Kept, unmodified, because migration 0025 (which created
        # it) is SHIPPED and forward-only -- see migration 0027's docstring,
        # which adds the index the drain query actually uses.
        Index("ix_domain_events_drain", "claimed_at", "occurred_at"),
        Index("ix_domain_events_org_type", "org_id", "event_type"),
        # Migration 0027: a partial index matching the drain query's real
        # predicate, `(org_id, processed_at IS NULL)`.
        Index(
            "ix_domain_events_org_unprocessed",
            "org_id",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. Set by the application.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"), default=None, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100))
    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[UUID] = mapped_column()
    # Only ever carries publishable content (spec §2.1) -- never a raw model
    # dump, which is how internal content leaks into an external channel.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Spec §2.1: NULLABLE on purpose and must stay so -- a Phase 16 alert event
    # has no human actor, and a NOT NULL would force a synthetic user into the
    # record.
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column()
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
    processed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column()


class AgentRelease(Base):
    """One downloadable agent artefact: a version, for one OS and architecture.

    NO `org_id`, and NO RLS policy, deliberately. The bytes are identical for
    every tenant -- an enrollment token is never inside an artefact, because
    `agent/packaging/README.md` forbids it ("one image build and the whole fleet
    shares one token") -- so a per-org copy would multiply storage for no
    isolation at all. What must be constrained is the RIGHT TO PUBLISH, which is
    RBAC.

    THE CONSEQUENCE, stated here because it inverts this file's own opening
    comment: for this one table, a missing `require_permission` on a route is not
    softened by a row filter failing closed at the database. There is no row
    filter. `tests/test_agent_release_model.py` pins the absence as deliberate,
    and the route tests lead with the authorization cases for the same reason.

    See `docs/modules/agent-distribution/spec.md` §4.2.
    """

    __tablename__ = "agent_releases"
    __table_args__ = (
        # Two artefacts for one (version, os, arch) is unresolvable: an install
        # command names exactly one object and nothing in the data chooses.
        UniqueConstraint(
            "version", "os", "arch", name="uq_agent_releases_version_os_arch"
        ),
        # The catalogue is listed newest-first and nothing else orders it.
        # Declared here as well as in migration 0059 because the model IS the
        # schema of record -- `tests/test_schema_drift.py` compares the two and
        # caught this index existing in the database and nowhere else.
        Index("ix_agent_releases_published", "published_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    version: Mapped[str] = mapped_column(String(64))
    os: Mapped[str] = mapped_column(String(16))  # linux | windows | darwin
    arch: Mapped[str] = mapped_column(String(16))  # amd64 | arm64
    storage_ref: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Hex digest computed by the SERVER over the bytes it received, never taken
    # from the uploader: a digest supplied alongside the bytes by the same party
    # verifies nothing. 64 chars because that is a sha256 hex digest, and a
    # truncated one fails as a mismatch an operator cannot explain rather than
    # as an error.
    sha256: Mapped[str] = mapped_column(String(64), default="")
    signed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(String(500), default="")
    # Maturity, set per version by the platform operator
    # (spec-self-update.md §6.1): `pilot` releases reach pilot rings only.
    channel: Mapped[str] = mapped_column(String(16), default="pilot", server_default="pilot")
    # Base64 Ed25519 signature by the offline release key (release workflow),
    # passed to agents verbatim. The server cannot verify it and does not try.
    release_signature: Mapped[str | None] = mapped_column(String(128), default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    published_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )


class DashboardLayout(Base):
    """One operator's own arrangement of the command centre.

    The operator, 2026-09-01: "main dashboard should be customizable with tiles
    also give me the ability to resize and order in whichever way i want."

    ONE ROW PER (ORG, USER), OVERWRITTEN IN PLACE. A layout is a preference,
    not an event: nothing reads a previous version, so the write is an upsert
    on the composite primary key rather than an append with a "current" flag to
    keep straight. Deleting the row IS "reset to default" — there is no
    NULL-versus-empty ambiguity to interpret, because absent means "this person
    has never arranged it" and `[]` is impossible (an empty layout is refused).

    `layout` IS A LIST OF `{id, size}` AND IS NOT VALIDATED AGAINST A WIDGET
    LIST, on either side of the wire, by design. The set of widgets is a
    property of the console BUILD, not of the schema or the server: a release
    that adds a tile must not need a migration, and a release that removes one
    must not strand every row that names it. So the server validates SHAPE
    (`app.api.v1.dashboard`) and the client resolves ids against the widgets it
    actually has — ignoring what it does not recognise, appending what the row
    does not mention. Both halves are load-bearing. Without the server's bound,
    anyone with a session could store an arbitrarily large document; without
    the client's tolerance, a stored row would blank somebody's dashboard on
    the first release that touched the widget list.

    NOT AUDITED. Invariant 4 covers privileged action — job dispatch,
    credential access, config and policy change. Where a person put a tile on
    their own screen is none of those, and writing it to the hash-chained log
    would dilute a record whose entire value is that everything in it matters.
    """

    __tablename__ = "dashboard_layouts"

    org_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    layout: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
