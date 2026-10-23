"""`fleet-posture` -- devices x health (spec §5.1).

`requires = {devices.read, health.read}`. It is the question every operator
opens the product to ask, and NO SINGLE MODULE ANSWERS IT: `health/api.py`
answers it per device, and `devices` answers it without health. That is what
earns it a place here rather than in either module (§0.12).

**THE NO-INFERENCE RULE, MADE STRUCTURAL (§2).** A device with no `agents` row
is `stale` and its `agent_status` reads `never_enrolled`. It is NEVER "fresh by
absence", and the join that produces it is a LEFT OUTER JOIN for exactly that
reason -- an inner join would drop the device entirely, and a fleet report whose
denominator quietly excludes the machines nobody has heard from is the worst
number this report could produce. The same holds for alerts: a device with no
open alert rows reads `0`/`0`, not "unknown", because absence of an open alert
IS the record ("current alert state", §2's table), whereas absence of an agent
row is absence of a record.

**ORDERING IS STABLE AND TOTAL: `(hostname, devices.id)` (§6.3).** Hostnames are
not unique in this schema -- `ix_devices_org_hostname` is a plain index, not a
unique one, and two machines imaged from the same template routinely share one.
Ordering on `hostname` alone would let PostgreSQL return tied rows in any order
it liked between two requests, so `offset` paging could skip one row and return
another twice. The `devices.id` tiebreaker is what makes paging sound;
`tests/test_reporting_fleet_posture.py` seeds a deliberate tie and pages through
in size-1 pages to pin it.

**EVERY STATEMENT CARRIES AN EXPLICIT `Device.org_id == org_id` (§6.4)**, in
ADDITION to RLS. RLS is the guarantee; the predicate is the thing a reviewer can
see, and it is the half a test can pin on the admin engine where RLS is not
doing the rejecting.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, and_, func, or_, select

from app.core.i18n import N_
from app.models import Agent, Device
from app.modules.health.models import HealthAlert
from app.modules.reporting.registry import ReportDefinition
from app.modules.reporting.service import DEFAULT_MAX_LIMIT

KEY = "fleet-posture"

# The health module's severity ladder, weakest first. RESTATED rather than
# imported: §1.3 has reporting read tables and not module code, so a runtime
# import of `app.modules.health.registry` would be the coupling this module
# exists to avoid. `tests/test_reporting_fleet_posture.py` asserts this tuple
# equals `health.registry.SEVERITIES`, so the restatement is a pinned copy with
# a drift alarm rather than a guess.
_SEVERITY_LADDER: tuple[str, ...] = ("info", "warning", "critical")

# `health_alert.state` -- 'active' | 'acknowledged' | 'muted' | 'cleared'. An
# acknowledged or muted alert is still OPEN: acknowledging is "I have seen it",
# muting is "not now", and neither is "it stopped happening". Only `cleared` is.
_CLEARED = "cleared"

# What `agent_status` reads for a device that has never enrolled. NOT a value
# `agents.status` can hold ('online' | 'offline'), on purpose: a reader must be
# able to tell "we have an agent and it is offline" from "there has never been
# an agent", and collapsing them into 'offline' is the inference §2 forbids.
_NEVER_ENROLLED = "never_enrolled"


class FleetPostureParams(BaseModel):
    """Query parameters. `extra="forbid"`, so an unknown query parameter is a
    422 rather than a filter silently ignored (§6.5) -- a misspelled
    `os_familly=windows` returning the whole fleet is a wrong number nobody
    would question."""

    model_config = ConfigDict(extra="forbid")

    os_family: str | None = Field(
        default=None, description=N_("Restrict to one OS family, e.g. 'windows'.")
    )
    device_group_id: UUID | None = Field(
        default=None, description=N_("Restrict to devices in one device group.")
    )
    stale_after_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=N_(
            "A device whose agent has not been in contact within this many days is "
            "reported stale. A device with no agent at all is always stale."
        ),
    )
    severity_at_least: Literal["info", "warning", "critical"] = Field(
        default="info",
        description=N_(
            "Only count open alerts at this severity or above. Does not filter the "
            "device list: a device with no qualifying alert still appears, with zero "
            "counts."
        ),
    )
    limit: int = Field(
        default=50, ge=1, le=DEFAULT_MAX_LIMIT, description=N_("Maximum rows to return.")
    )
    offset: int = Field(default=0, ge=0, description=N_("Rows to skip before the first returned."))


class FleetPostureRow(BaseModel):
    """One device."""

    device_id: UUID = Field(description="The device this row is about.")
    hostname: str = Field(description="The device's hostname as last reported by its agent.")
    os_family: str = Field(description="windows | linux | macos.")
    os_version: str = Field(description="The OS version string the agent last reported.")
    device_group_id: UUID | None = Field(
        description="The device group this device belongs to, or null if it is ungrouped."
    )
    agent_status: str = Field(
        description=(
            "online | offline as the gateway last recorded it, or 'never_enrolled' when "
            "no agent has ever enrolled for this device. Never inferred."
        )
    )
    last_seen_at: datetime | None = Field(
        description="When the agent was last in contact, or null if it never has been."
    )
    stale: bool = Field(
        description=(
            "True when the agent has not been in contact within `stale_after_days`, OR "
            "when there is no agent at all. A device is never fresh by absence."
        )
    )
    open_alerts_critical: int = Field(
        description="Open critical health alerts (active, acknowledged or muted; not cleared)."
    )
    open_alerts_warning: int = Field(description="Open warning health alerts.")
    newest_alert_opened_at: datetime | None = Field(
        description="When the most recent still-open alert opened, or null if there are none."
    )


def _open_alerts(org_id: UUID, params: FleetPostureParams) -> Any:
    """Open alerts per device, aggregated, as a subquery.

    Aggregating BEFORE the join to `devices` rather than grouping the whole
    statement: a device joined row-per-alert and then grouped would make every
    `devices` column a `GROUP BY` term, and `totals`' device count would be a
    count of groups -- correct today and one added column away from silently
    becoming a count of alerts.
    """
    counted = _SEVERITY_LADDER[_SEVERITY_LADDER.index(params.severity_at_least) :]
    return (
        select(
            HealthAlert.device_id.label("device_id"),
            func.count()
            .filter(HealthAlert.severity == "critical")
            .label("open_alerts_critical"),
            func.count()
            .filter(HealthAlert.severity == "warning")
            .label("open_alerts_warning"),
            func.max(HealthAlert.opened_at).label("newest_alert_opened_at"),
        )
        .where(
            HealthAlert.org_id == org_id,
            HealthAlert.state != _CLEARED,
            HealthAlert.severity.in_(counted),
        )
        .group_by(HealthAlert.device_id)
        .subquery()
    )


def _base(*, org_id: UUID, params: FleetPostureParams) -> Select[Any]:
    stale_before = datetime.now(UTC) - timedelta(days=params.stale_after_days)
    alerts = _open_alerts(org_id, params)
    stale = or_(
        Agent.id.is_(None),
        Agent.last_seen_at.is_(None),
        Agent.last_seen_at < stale_before,
    )
    stmt = (
        select(
            Device.id.label("device_id"),
            Device.hostname.label("hostname"),
            Device.os_family.label("os_family"),
            Device.os_version.label("os_version"),
            Device.device_group_id.label("device_group_id"),
            func.coalesce(Agent.status, _NEVER_ENROLLED).label("agent_status"),
            Agent.last_seen_at.label("last_seen_at"),
            stale.label("stale"),
            func.coalesce(alerts.c.open_alerts_critical, 0).label("open_alerts_critical"),
            func.coalesce(alerts.c.open_alerts_warning, 0).label("open_alerts_warning"),
            alerts.c.newest_alert_opened_at.label("newest_alert_opened_at"),
        )
        .select_from(Device)
        # OUTER, both of them. See the module docstring: an inner join here
        # drops exactly the devices this report exists to surface.
        .outerjoin(Agent, and_(Agent.device_id == Device.id, Agent.org_id == Device.org_id))
        .outerjoin(alerts, alerts.c.device_id == Device.id)
        # §6.4: explicit, IN ADDITION to RLS.
        .where(Device.org_id == org_id)
    )
    if params.os_family is not None:
        stmt = stmt.where(Device.os_family == params.os_family)
    if params.device_group_id is not None:
        stmt = stmt.where(Device.device_group_id == params.device_group_id)
    # Stable and TOTAL (§6.3). The `Device.id` tiebreaker is load-bearing.
    return stmt.order_by(Device.hostname, Device.id)


def build(*, org_id: UUID, params: FleetPostureParams) -> Select[Any]:
    return _base(org_id=org_id, params=params)


def build_totals(*, org_id: UUID, params: FleetPostureParams) -> Select[Any]:
    """Aggregates over the WHOLE match, not the page (§6.3).

    Built from the same `_base` statement the rows come from, so a filter can
    never apply to one and not the other -- the failure mode where a totals
    query hand-written alongside the row query drifts from it, and the number a
    dashboard renders largest is the one that is wrong.
    """
    matched = _base(org_id=org_id, params=params).subquery()
    return select(
        func.count().label("devices"),
        func.count().filter(matched.c.stale).label("stale_devices"),
        func.count()
        .filter((matched.c.open_alerts_critical + matched.c.open_alerts_warning) > 0)
        .label("devices_with_open_alerts"),
    ).select_from(matched)


DEFINITION = ReportDefinition(
    key=KEY,
    summary=N_("Every device with its agent contact state and its open health alerts."),
    description=N_(
        "One row per device: identity, OS, device group, agent status and last contact, "
        "whether that contact is stale, and the count of still-open critical and warning "
        "health alerts. Devices with no agent are reported as stale and never_enrolled "
        "rather than omitted, and devices with no open alerts are reported with zero "
        "counts rather than omitted -- so the device total is the fleet, not the subset "
        "that happens to have reported. Computed live; it is a statement about the "
        "record at `meta.generated_at`."
    ),
    params=FleetPostureParams,
    row_model=FleetPostureRow,
    sources=frozenset({"devices", "agents", "health_alert"}),
    build=build,
    build_totals=build_totals,
    max_limit=DEFAULT_MAX_LIMIT,
    export_columns=(
        "device_id",
        "hostname",
        "os_family",
        "os_version",
        "device_group_id",
        "agent_status",
        "last_seen_at",
        "stale",
        "open_alerts_critical",
        "open_alerts_warning",
        "newest_alert_opened_at",
    ),
    # Agent-supplied free text, and the only two columns here that are. Every
    # other column is a UUID, a timestamp, a bool, an int, or a fixed-set enum
    # the server itself produced -- running the escaper over those would corrupt
    # them for a machine reader, which `app/core/csv_export.py`'s own docstring
    # explains at length.
    export_free_text=frozenset({"hostname", "os_version"}),
    export_row_cap=50_000,
    # Current-state sources only: `devices`, `agents` and open `health_alert`
    # rows all describe now. Re-running this report a minute later may return
    # different numbers because the FLEET changed, not because the record of a
    # past period was rewritten -- which is the distinction `reproducible`
    # draws. `sla-attainment` is the report where this is False.
    reproducible=True,
)
