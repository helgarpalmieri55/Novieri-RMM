"""The two approved-action tables (docs/modules/endpoint-health-alerting/spec.md
§2.9, §2.10).

KERNEL TABLES, NOT HEALTH TABLES. Phase 17's AI-automation layer shares this
registry (`docs/06-roadmap.md`: "one registry, one dial, one audit"), so it must
not live under `app/modules/health/`. The registry's BEHAVIOUR -- the closed
maps, `is_destructive`, `render_params_template`, the org-scoped reads -- lives
in `app/core/approved_actions.py`, which re-exports these two classes; import
from there.

WHY THE DECLARATIVE CLASSES ARE HERE AND NOT IN `app/core/`. Spec §2.9 says the
table "lives in `backend/app/core/approved_actions.py` with its model beside the
other kernel models". Those two halves contradict each other in this codebase:
the other kernel models are in `app/models/` (`core.py`, `security.py`,
`vault.py`), and a declarative model defined under `app/core/` cannot be
registered in `app/models/__init__.py` without a genuine import cycle --
`app/core/approved_actions.py` needs `app.models.base.Base`, importing which
executes `app/models/__init__.py`, which would be mid-way through importing
`app.core.approved_actions`. Verified, not assumed: the cycle raises
`ImportError: cannot import name 'ApprovedAction' from partially initialized
module`. The models therefore sit beside the other kernel models, exactly as the
second half of §2.9 says, and `app/core/approved_actions.py` remains the single
module a caller imports the concept from.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


def _utcnow() -> datetime:
    return datetime.now(UTC)



RISK_CLASSES = ("low", "medium", "high")
AUTONOMY_LEVELS = ("suggest", "require_approval", "autonomous")
# `max_target_scope IN ('device')` in v1. The COLUMN exists so Phase 17 can
# widen it by a reviewed migration rather than by inventing a second concept.
MAX_TARGET_SCOPES = ("device",)

# ---------------------------------------------------------------------------
# Ceilings on the rate fuses (spec §8.3)
# ---------------------------------------------------------------------------
#
# WHY A CEILING AT ALL. `ck_approved_action_fuses_positive` required only `> 0`,
# so `max_per_org_per_hour = 1_000_000` was a storable value. §8.4's claim that
# "fleet-wide is structurally impossible" rests on `_dispatch_remediation` taking
# a single device `UUID` -- which makes ONE JOB device-scoped and says nothing
# about HOW MANY. The sweep dispatches up to 500 rows every 30 seconds, so an
# unbounded fuse turns "one job, one device" into 60,000 machine-decided jobs an
# hour with the structural claim still technically true. An operator integer with
# no upper bound is not a fuse; it is a text field next to a fuse.
#
# WHAT THE CEILING DOES AND DOES NOT BUY, stated rather than implied: it does NOT
# make reaching every device in a small org impossible. It makes the number
# BOUNDED, bounded by something in this codebase rather than by taste, and
# bounded low enough that `actions.py`'s `.limit(max_per_org_per_hour + 5)` stays
# a small scan instead of a million-row one.

# Anchored to `actions.execute_pending_actions`' own `limit=500`: an org may not
# be authorized for more machine dispatches of one job type in an HOUR than the
# sweep can emit in a single CYCLE.
MAX_PER_ORG_PER_HOUR = 500
# At the default 3600s cooldown a device can be reached at most 24 times a day,
# so this is already four times the largest value the shipped defaults can
# produce; it bounds one endpoint's exposure at roughly four attempts an hour.
MAX_PER_DEVICE_PER_DAY = 100
# 30 days. A cooldown longer than a month is indistinguishable from "off" and
# says so less clearly, and `actions._fuse_breach` puts this value straight into
# a `timedelta`, which raises `OverflowError` on a large enough integer.
MAX_COOLDOWN_S = 30 * 24 * 3600


class ApprovedAction(Base):
    """One thing a machine is permitted to run, and under what governance.

    Every column below must match the migration exactly in type, length and
    nullability -- `test_schema_drift.py` compares `Base.metadata` to the live
    schema. It does NOT compare constraints or indexes, so the constraints
    restated here are for the reader and for a future autogenerate; the
    migration is what actually creates them.

    ORG-SCOPED WITH NO GLOBAL ROWS. `org_id` is NOT NULL. A global approved
    action would be a cross-tenant command path into every fleet, and a "shared
    library of safe remediations" is a seeding concern (copy per org), not a
    row-visibility concern.
    """

    __tablename__ = "approved_action"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_approved_action_org_key"),
        CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')",
            name="ck_approved_action_risk_class",
        ),
        CheckConstraint(
            "autonomy IN ('suggest', 'require_approval', 'autonomous')",
            name="ck_approved_action_autonomy",
        ),
        CheckConstraint(
            "max_target_scope IN ('device')",
            name="ck_approved_action_max_target_scope",
        ),
        # THE HARD GATE (spec §8.3, §2.9). "Nothing destructive auto-fires", as
        # a database constraint rather than as application logic: a future
        # persona cannot relax it in Python, it takes a migration, which a
        # reviewer sees.
        #
        # NOTE WHAT THIS CONSTRAINT DOES NOT SAY. It bounds `max_target_scope`,
        # which is the scope of ONE dispatch; the number of dispatches is bounded
        # by the rate fuses below, and until those acquired an upper bound the
        # "nothing fleet-wide" half of the old wording was not enforced anywhere.
        CheckConstraint(
            "NOT (autonomy = 'autonomous' AND (destructive OR risk_class = 'high' "
            "OR max_target_scope <> 'device'))",
            name="ck_approved_action_autonomous_is_bounded",
        ),
        # Version pinning is the point (§2.9): `scripts` is versioned, and
        # without the pin "approve the disk-cleanup script" silently means
        # "approve whatever that script says tomorrow".
        CheckConstraint(
            "(job_type = 'script.run') = (script_id IS NOT NULL AND script_version IS NOT NULL)",
            name="ck_approved_action_script_pin",
        ),
        CheckConstraint(
            f"cooldown_s >= 0 AND cooldown_s <= {MAX_COOLDOWN_S}",
            name="ck_approved_action_cooldown_bounded",
        ),
        # BOUNDED AT BOTH ENDS, and the upper bound is the half that matters.
        # `> 0` alone left `max_per_org_per_hour` an unbounded operator integer,
        # which is what "nothing fleet-wide auto-fires" was actually resting on.
        CheckConstraint(
            f"max_per_device_per_day > 0 AND max_per_device_per_day <= "
            f"{MAX_PER_DEVICE_PER_DAY} AND max_per_org_per_hour > 0 AND "
            f"max_per_org_per_hour <= {MAX_PER_ORG_PER_HOUR}",
            name="ck_approved_action_fuses_bounded",
        ),
        Index("ix_approved_action_org_active", "org_id", "active"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # Validated at WRITE time against `app.jobs.registry.registered_names()` by
    # the service layer (§2.9) -- an unregistered type is a 400, never an
    # `UnknownJobType` discovered at dispatch with an alert already open.
    job_type: Mapped[str] = mapped_column(String(100))
    params_template: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    script_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scripts.id"), default=None
    )
    script_version: Mapped[int | None] = mapped_column(Integer, default=None)
    risk_class: Mapped[str] = mapped_column(String(8))
    # DERIVED AND STORED, never accepted from a request body: `is_destructive`
    # above is the only thing that may set it, and the gate CHECK depends on it.
    destructive: Mapped[bool] = mapped_column(Boolean)
    max_target_scope: Mapped[str] = mapped_column(String(8), server_default="device")
    autonomy: Mapped[str] = mapped_column(String(16))
    # The persona dimension Phase 17 requires, present from day one. v1 rows
    # carry ["health"]; the AI layer adds "end_user"/"technician"/"admin" to THE
    # SAME ROWS. No parallel table, no fork (§8.5).
    personas: Mapped[list[str]] = mapped_column(JSONB, default=lambda: ["health"])
    # The rate fuses are evaluated against `jobs`, not against a counter table
    # (§2.9): `jobs` is already the durable, audited record of every dispatch,
    # and a second counter would be a second truth. Bounded ABOVE as well as
    # below by `ck_approved_action_fuses_bounded` -- see the ceilings above.
    cooldown_s: Mapped[int] = mapped_column(Integer, server_default="3600", default=3600)
    max_per_device_per_day: Mapped[int] = mapped_column(Integer, server_default="3", default=3)
    max_per_org_per_hour: Mapped[int] = mapped_column(Integer, server_default="20", default=20)
    active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class ApprovedActionScriptExemption(Base):
    """"This exact script body is not destructive" -- granted per exact version.

    KEYED TO THE `(script_id, script_version)` PAIR, NOT TO AN APPROVED ACTION
    (spec §2.10). §8.4's own wording -- "granted per exact version ... editing
    the script mints a new version and the exemption does not follow it" -- is a
    property of a script version, and two approved actions naming the same
    pinned version share one exemption rather than needing two identical grants.

    Rejected: an `exempt` boolean on `approved_action`. It would be per approved
    action rather than per script version, so two actions pinning the same
    version could disagree about whether that exact body is destructive -- one
    derivation function producing two answers, the thing §8.4 exists to make
    impossible.

    THE COST, STATED: an exemption outlives the approved action that motivated
    it, so a later action pinning the same pair inherits a grant made for a
    different purpose. Accepted rather than fixed, because expiring exemptions
    with their actions would make the same pair destructive or not depending on
    lookup order. The mitigation is visibility -- the exemption list is a
    first-class view showing script name, version, grantor and reason.

    THE SECOND COST, AND WHAT PAYS IT: because the row is shared, granting or
    revoking it changes the DERIVED `destructive` of every approved action
    pinning the pair, while the caller names only one of them. Nothing
    downstream can detect the difference -- `approved_action.destructive` is a
    STORED column, `ck_approved_action_autonomous_is_bounded` reads that stored
    column, and the dispatcher branches on `autonomy` alone -- so a sibling left
    behind is an arbitrary-code `script.run` still stored as non-destructive and
    still eligible for `autonomous`. `health.approved_actions` therefore
    re-derives and audits EVERY action pinning the pair on both the grant and
    the revoke path; that sweep is what makes the sharing safe, and it is pinned
    by `test_health_approved_actions.py`.
    """

    __tablename__ = "approved_action_script_exemption"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "script_id",
            "script_version",
            name="uq_approved_action_script_exemption",
        ),
        CheckConstraint(
            "script_version > 0", name="ck_approved_action_script_exemption_version_positive"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # ON DELETE CASCADE: deleting a script must not leave an exemption a later
    # script could inherit by id reuse (§2.10).
    script_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"))
    script_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500), default="")
    granted_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(default=_utcnow)
