"""The closed check-type registry
(`docs/modules/endpoint-health-alerting/spec.md` §4.1).

WHY THIS IS DATA AND NOT CONFIGURATION (§0.1). A health check is a row drawn
from a closed registry of builtin types, never a free-form expression. Two
reasons, and the second is the load-bearing one: an evaluator is an interpreter
with an unbounded cost profile, and §0.5's idle claim is only checkable because
the work per tick is a fixed, enumerable set of counter reads; and a closed set
is what lets the SERVER know, per OS, whether a check is even answerable --
which is what §0.7's `unavailable` state depends on. Adding a check type is a
code change on BOTH SIDES (this file + `agent/internal/capabilities/health/`)
plus an edit to §4.1, deliberately not a configuration row. Both sides now live
in one repository, which does not make it one change: a deployed agent is older
than the control plane and will be asked to run a check type it has never heard
of, so a new type is additive on the endpoint before it is required by the
server (CLAUDE.md, "the wire protocol is still a contract").

THIS FILE AND THE DATABASE MUST AGREE. `ck_health_check_type`,
`ck_health_check_comparator` and `ck_health_check_os_family` in migration 0042
restate these three sets in SQL, because SQL cannot consult a Python dict.
`tests/test_health_check_registry.py` compares the two BOTH WAYS against
`pg_get_constraintdef`, so neither list can drift: a type in Python with no SQL
member is a check nobody can store, and a member left in SQL after Python drops
it is a check the agent will be asked to run and cannot.

`value_kind` DESCRIBES THE REPORTED VALUE, AND IT SELECTS THE THRESHOLD COLUMN.
§2.2 gives `health_check` exactly two threshold columns (`threshold_num`,
`threshold_bool`) and §4.1 gives three value kinds, so `enum` has no column of
its own. Resolved here rather than by adding a third column (which §2.2 fixes)
or by dropping the `enum` kind (which would lose that the agent reports the
literal string `running`, per §5.1's example): an `enum` type names exactly two
members, `enum_values[0]` is the one `threshold_bool = true` denotes, and
`render_threshold` emits the MEMBER NAME on the wire so the agent compares
like with like. A third member would need a schema change, which §0.1 already
requires of any registry change. Recorded rather than assumed -- see the build
report for this task.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.core.i18n import N_, tr

__all__ = [
    "CADENCES",
    "CHECK_TYPES",
    "COMPARATORS",
    "MAX_CHECKS_PER_PROFILE",
    "NUMERIC_COMPARATORS",
    "OS_FAMILIES",
    "SEVERITIES",
    "VALUE_KINDS",
    "CheckType",
    "agent_check_type_names",
    "check_type",
    "is_available_on",
    "render_threshold",
    "threshold_column_for",
]

ValueKind = Literal["numeric", "boolean", "enum"]
Cadence = Literal["tick", "posture"]
EvaluatedBy = Literal["agent", "server"]

OS_FAMILIES: tuple[str, ...] = ("windows", "linux", "macos")
COMPARATORS: tuple[str, ...] = ("gt", "gte", "lt", "lte", "eq", "neq", "is")
VALUE_KINDS: tuple[str, ...] = ("numeric", "boolean", "enum")
CADENCES: tuple[str, ...] = ("tick", "posture")
SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")

# The comparators a numeric check may carry. Boolean and enum checks take `is`
# and nothing else: `gt` on a boolean is not a mistake the database can catch
# (both are legal members of `ck_health_check_comparator`), so this map is the
# only thing standing between "disk encryption > true" and an agent asked to
# evaluate it.
NUMERIC_COMPARATORS: frozenset[str] = frozenset({"gt", "gte", "lt", "lte", "eq", "neq"})
_STATE_COMPARATORS: frozenset[str] = frozenset({"is"})

# §0.11, and the number the 32-check cap trigger enforces. Stated here as well
# as in the migration because Task 5's service refuses the 33rd check with a
# typed error rather than letting the trigger produce an `IntegrityError` the
# API would have to translate.
MAX_CHECKS_PER_PROFILE = 32

_ALL_OS: frozenset[str] = frozenset(OS_FAMILIES)


@dataclass(frozen=True)
class CheckType:
    """One row of §4.1's table.

    `os_families` is the set on which the check can be ANSWERED. A device whose
    family is absent reports `unavailable` (§0.7) -- which is different from
    failing, and is why resolution (§3) filters the pushed set by OS rather
    than shipping every check and letting the agent shrug.
    """

    name: str
    value_kind: ValueKind
    cadence: Cadence
    os_families: frozenset[str]
    legal_comparators: frozenset[str]
    selector_meaning: str = ""
    evaluated_by: EvaluatedBy = "agent"
    enum_values: tuple[str, ...] = field(default=())


def _numeric(
    name: str,
    *,
    cadence: Cadence = "tick",
    os_families: frozenset[str] = _ALL_OS,
    selector_meaning: str = "",
    evaluated_by: EvaluatedBy = "agent",
) -> CheckType:
    return CheckType(
        name=name,
        value_kind="numeric",
        cadence=cadence,
        os_families=os_families,
        legal_comparators=NUMERIC_COMPARATORS,
        selector_meaning=selector_meaning,
        evaluated_by=evaluated_by,
    )


def _boolean(
    name: str,
    *,
    cadence: Cadence = "posture",
    os_families: frozenset[str] = _ALL_OS,
    selector_meaning: str = "",
    evaluated_by: EvaluatedBy = "agent",
) -> CheckType:
    return CheckType(
        name=name,
        value_kind="boolean",
        cadence=cadence,
        os_families=os_families,
        legal_comparators=_STATE_COMPARATORS,
        selector_meaning=selector_meaning,
        evaluated_by=evaluated_by,
    )


CHECK_TYPES: dict[str, CheckType] = {
    t.name: t
    for t in (
        _numeric("cpu.busy_pct"),
        _numeric("memory.used_pct"),
        _numeric("disk.free_pct", selector_meaning=N_("mount/volume, '*' = worst")),
        _numeric("disk.free_bytes", selector_meaning=N_("mount/volume, '*' = worst")),
        CheckType(
            name="service.state",
            value_kind="enum",
            cadence="tick",
            os_families=_ALL_OS,
            legal_comparators=_STATE_COMPARATORS,
            selector_meaning=N_("service name"),
            # Index 0 is the member `threshold_bool = true` denotes. See the
            # module docstring for why an enum lives in a boolean column.
            enum_values=("running", "stopped"),
        ),
        _boolean("process.present", cadence="tick", selector_meaning=N_("process name")),
        _boolean(
            "disk.smart_ok",
            os_families=frozenset({"windows", "linux"}),
            selector_meaning=N_("device path, '*' = all"),
        ),
        _boolean("posture.disk_encryption"),
        # Windows Security Center only in v1 (§4.1). "AV health" on Linux/macOS
        # has no vendor-neutral answer and inventing one produces a check that
        # is confidently wrong.
        _boolean("posture.antivirus_healthy", os_families=frozenset({"windows"})),
        _boolean("posture.firewall_enabled"),
        # Counters read from the security-logs capability's EXISTING OS
        # subscription (§4.1 note). They open no subscription of their own;
        # with security-logs disabled they report `unavailable`.
        _numeric("eventlog.disk_error"),
        _numeric("eventlog.service_crash"),
        _numeric("eventlog.kernel_fault"),
        # THE FIRST SERVER-EVALUATED CHECK (§0.6): an agent that is offline
        # cannot report that it is offline. `cadence` is inert for this entry
        # -- it is never in a policy push -- and is recorded as `posture`
        # because the sweep that evaluates it is the slow one. It carries a
        # legal value rather than None so no consumer needs a special case.
        _numeric("agent.offline", cadence="posture", evaluated_by="server"),
        # Backup protection (backup/spec-server-backup.md §8.1), evaluated by
        # the backup module's own sweep from its tables; never in a push.
        _numeric("backup.success_age_h", cadence="posture", evaluated_by="server"),
        _boolean("backup.last_run_failed", evaluated_by="server"),
        _numeric("backup.offsite_lag_h", cadence="posture", evaluated_by="server"),
        _numeric("backup.restore_test_age_d", cadence="posture", evaluated_by="server"),
        # A scheduled script's latest run on this device failed
        # (app-deploy spec §9). Evaluated when the run's result arrives.
        _boolean(
            "script.policy_failed",
            evaluated_by="server",
            selector_meaning=N_("schedule name, empty = any schedule"),
        ),
    )
}


def check_type(name: str) -> CheckType:
    """The registry entry for `name`. Raises `KeyError` for an unregistered
    name -- callers that accept operator input validate against `CHECK_TYPES`
    first and raise `HealthValidationError`; this is the internal accessor."""
    return CHECK_TYPES[name]


def is_available_on(name: str, os_family: str) -> bool:
    """Can this check be ANSWERED on that OS? A `False` here means the check
    reports `unavailable` (§0.7), which is different from failing, and is why
    §3 filters it out of the pushed set server-side."""
    return os_family in CHECK_TYPES[name].os_families


def agent_check_type_names() -> frozenset[str]:
    """The types that may appear in a `health.policy.apply` push.

    One list rather than an `if entry.evaluated_by == "server"` at each call
    site: the exclusion has to hold for the policy build, the reconciliation
    sweep and the effective-profile view, and three copies is three chances to
    ship `agent.offline` to an agent that cannot evaluate it.
    """
    return frozenset(n for n, t in CHECK_TYPES.items() if t.evaluated_by == "agent")


def threshold_column_for(value_kind: str) -> str:
    """Which `health_check` column holds this kind's threshold.

    SQL cannot consult this registry, so `ck_health_check_one_threshold` can
    only enforce "exactly one of the two is set" -- pairing the RIGHT one with
    the check type is this function plus Task 5's validation, and it is the only
    thing standing between a boolean check and a numeric threshold.
    """
    return "threshold_num" if value_kind == "numeric" else "threshold_bool"


def render_threshold(
    entry: CheckType, *, threshold_num: Decimal | float | int | None, threshold_bool: bool | None
) -> str:
    """The threshold as it travels on the wire (§5.2: `threshold` is a string,
    for the same reason `observed_value` is -- see §2.4).

    Numeric renders the number; boolean renders `true`/`false`; enum renders the
    MEMBER NAME, so the agent compares its reported `running`/`stopped` against
    the same alphabet rather than against a boolean it would have to map.
    """
    if entry.value_kind == "numeric":
        if threshold_num is None:
            raise ValueError(
                tr("{check_type} is numeric and carries no threshold_num", check_type=entry.name)
            )
        rendered = format(Decimal(str(threshold_num)).normalize(), "f")
        return rendered
    if threshold_bool is None:
        raise ValueError(
            tr(
                "{check_type} is {value_kind} and carries no threshold_bool",
                check_type=entry.name,
                value_kind=entry.value_kind,
            )
        )
    if entry.value_kind == "enum":
        return entry.enum_values[0] if threshold_bool else entry.enum_values[1]
    return "true" if threshold_bool else "false"
