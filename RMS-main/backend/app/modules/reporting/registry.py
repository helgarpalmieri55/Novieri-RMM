"""The report definition registry (spec §3), shaped after
`app/modules/ai_admin/tools/registry.py` and for the same stated reason:
*"whatever compromises the database would otherwise choose the agent's
capabilities."* A saved-query table would put the reporting surface's shape
under `app_user`'s INSERT rights; a Python module compiled into the image is not
writable by anything the database can reach.

THE PROPERTY THIS FILE ESTABLISHES is that **a report's permission requirement
is DERIVED from the tables it reads, and a definition author cannot understate
it.** `requires` is `field(init=False)`: it is not a constructor parameter, so
`ReportDefinition(..., requires=frozenset())` is a `TypeError`, and the only
value it can ever hold is `frozenset(TABLE_PERMISSIONS[t] for t in sources)`.
`sources.py` closes the other half by verifying that declaration against the
compiled SQL; the two together are what make §0.4's guarantee -- *"an operator
never sees through a report what the API would refuse them directly"* -- a
checkable property rather than an intention.

WHY `TABLE_PERMISSIONS` NAMES THE *WEAKEST* CODE THAT GOVERNS *ANY* COLUMN OF A
TABLE (§4.2). The residual risk this map has to absorb is a table whose
route-level gate is finer than its table-level mapping -- a module that serves
one table under two codes depending on the column selected. `license` is exactly
that shape (`licenses.read` for the catalogue, `licenses.keys.read` for the
decrypted key). Mapping such a table to its STRONGEST code would silently
over-require and make honest reports unrunnable; mapping it to its weakest and
putting the secret columns on `_NEVER_A_COLUMN` keeps the conjunction honest at
the column that actually matters.

WHY A DENYLIST AT ALL, GIVEN THAT `TABLE_PERMISSIONS` IS AN ALLOWLIST. The same
answer `ai_admin` gives: a path can be renamed, but the intent of "never through
a report" is stickier than any one name, and the day someone adds `credentials`
to `TABLE_PERMISSIONS` "because the credential *metadata* is harmless" the
denylist is the thing that says no. `_NEVER_A_SOURCE ∩ TABLE_PERMISSIONS` is
asserted empty, so the two can never disagree silently, and
`test_denylisted_sources_are_real_tables` is what stops the tripwire rotting
into pointing at nothing.

**NO TABLE IS DEFINED HERE.** This module owns no facts (§0.3). Everything below
is a mapping over tables OTHER modules own.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select

from app.core.i18n import N_
from app.modules.reporting.errors import DenylistedSource, UndeclaredSource

# --------------------------------------------------------------------------- #
# Table -> the read permission code that governs it (§3, §4.2).
#
# EVERY VALUE IS A LIVE KEY OF `app.core.rbac.PERMISSIONS` AND EVERY KEY IS A
# LIVE TABLE, both asserted by `tests/test_reporting_registry.py`. Both
# directions are a liveness check, not a formality: a map entry naming a renamed
# table maps nothing, and a code that no longer exists cannot be granted, so
# either drift turns the conjunction into an assertion about a name.
#
# NOTE ON `inventory`: there is no `inventory.*` permission code in this
# codebase. `app/modules/inventory/api.py` gates all six of its routes --
# including both CSV exports -- on `devices.read`. If a later report reads
# `inventory_snapshots` / `installed_software` / `hardware_components` /
# `inventory_changes`, they map to `devices.read`, NOT to an invented code.
# --------------------------------------------------------------------------- #
TABLE_PERMISSIONS: Mapping[str, str] = MappingProxyType(
    {
        # devices (§5.1, §5.2, §5.3)
        "devices": "devices.read",
        "agents": "devices.read",
        "device_groups": "devices.read",
        # kernel. `jobs` is here because a succeeded job IS the durable record
        # that a capability ran on a device, and no module keeps a second copy
        # of that fact -- `patch-compliance` reads it for `last_scan_at`
        # (§5.2, amended). `jobs.read` is what the jobs API gates the table on,
        # so a report shows nothing that API would refuse. The columns that API
        # DOES refuse -- `params` is redacted per job type when it carries a
        # bearer credential, and the signing material is never returned at all
        # -- are on `_NEVER_A_COLUMN` below, which is exactly the shape §4.2
        # describes: a table whose route-level gate is finer than its
        # table-level mapping.
        "jobs": "jobs.read",
        # health (§5.1)
        "health_alert": "health.read",
        # patch (§5.2)
        "device_patch_status": "patch.read",
        "patches": "patch.read",
        "patch_approvals": "patch.read",
        "patch_policies": "patch.read",
        # backup (§5.3)
        "backup_repositories": "backup.read",
        "backup_policies": "backup.read",
        "backup_points": "backup.read",
        "backup_runs": "backup.read",
        "backup_point_copies": "backup.read",
        # service desk (§5.4)
        "ticket": "ticket.read",
        "ticket_queue": "ticket.read",
        # sla (§5.4)
        "ticket_sla": "sla.read",
    }
)

# --------------------------------------------------------------------------- #
# Table -> the module that owns it, and what its timestamps MEAN (§2, §6.3).
#
# `meta.sources` is built from these, so a report says which module each figure
# came from and the recency it inherits. This matters more than it looks: "12
# devices have no backup in 30 days" computed from `backup_points` says nothing
# about devices whose agent has been offline for 60 days, and a report that does
# not surface its own staleness invites exactly that misreading.
#
# Keyed by TABLE, not by module, so the mapping is derived from what a
# definition actually declares rather than hand-listed per report -- a report
# that gains a source gains its recency note with it.
# `tests/test_reporting_runner.py` asserts both maps cover `TABLE_PERMISSIONS`
# exactly, so a table added to one and not the others is a red test rather than
# a `KeyError` on someone's first request.
# --------------------------------------------------------------------------- #
TABLE_MODULE: Mapping[str, str] = MappingProxyType(
    {
        "devices": "devices",
        "agents": "devices",
        "device_groups": "devices",
        "jobs": "jobs",
        "health_alert": "health",
        "device_patch_status": "patch",
        "patches": "patch",
        "patch_approvals": "patch",
        "patch_policies": "patch",
        "backup_repositories": "backup",
        "backup_policies": "backup",
        "backup_points": "backup",
        "backup_runs": "backup",
        "backup_point_copies": "backup",
        "ticket": "service_desk",
        "ticket_queue": "service_desk",
        "ticket_sla": "sla",
    }
)

_RECENCY_BY_MODULE: Mapping[str, str] = MappingProxyType(
    {
        "devices": N_(
            "as of each agent's last contact (`agents.last_seen_at`, updated by the "
            "gateway on agent traffic); a device with no agent row has never reported"
        ),
        "jobs": N_(
            "as of each job's own completion (`jobs.finished_at`, written by "
            "`JobEngine.complete` when the handler returns); a job that has not "
            "reached a terminal state carries none"
        ),
        "health": N_(
            "current alert state: open or cleared as of the last check tick, never a "
            "history of alerts that have since closed"
        ),
        # NOT `device_patch_status.detected_at`, which this string named until
        # 2026-08-21 and which is not what it sounds like: it is stamped when a
        # status ROW IS FIRST CREATED and no later scan rewrites it, so on a
        # device whose patch set has not changed it dates the enrolment rather
        # than the last measurement. A reader trusts this line; it is now what
        # `patch-compliance` actually reads.
        "patch": N_(
            "current per-device patch state, as last rewritten by a scan; WHEN that "
            "scan ran is a `jobs` fact (`patch.scan`, succeeded), not a "
            "`device_patch_status` one -- `detected_at` dates a patch's first "
            "detection on the device and never moves again"
        ),
        "backup": N_("as of each device's last completed run (`backup_points.taken_at`)"),
        "service_desk": N_("current, and MUTABLE -- a ticket edited today changes this figure"),
        "sla": N_(
            "current, and MUTABLE BY DESIGN -- a human re-triage recomputes a deadline "
            "from ticket start, so a ticket that read 'met' last month can read "
            "'breached' today"
        ),
    }
)


def source_recency(table: str) -> str:
    """What the timestamps in `table` mean, for `meta.sources` (§2, §6.3)."""
    return _RECENCY_BY_MODULE[TABLE_MODULE[table]]


# --------------------------------------------------------------------------- #
# Tables no report may read AT ANY GATE (§3, §8, §0.9).
#
# These are not "tables we have not mapped yet" -- they are tables whose
# contents are reconnaissance or key material regardless of who is asking:
# the vault surface in its entirety (metadata included, for the same
# conservative reason `ai_admin` gives -- enumeration is reconnaissance), the
# webhook/notification egress configuration (`app/core/egress.py`'s SSRF
# surface), and the email-intake mailbox configuration.
#
# `audit_events` is NOT here as a table: §0.9 permits a future report to read it
# for AGGREGATES AND IDENTIFIERS ONLY, and forbids the chain columns -- so the
# prohibition it needs is column-level, and lives on `_NEVER_A_COLUMN`. No v1
# report reads it at all (§5.5).
# --------------------------------------------------------------------------- #
_NEVER_A_SOURCE: frozenset[str] = frozenset(
    {
        "credentials",
        "org_vault_keys",
        "enrollment_tokens",
        "notification_channels",
        "webhook_allowlist_entries",
        "email_mailbox",
        "inbound_message",
    }
)

# --------------------------------------------------------------------------- #
# `table.column` tails no report may put in `export_columns` or a row model
# (§4.2, §0.8, §0.9). The column-level half of the denylist, for tables whose
# route-level gate is finer than their table-level mapping.
#
# Every entry resolves to a LIVE `table.column` -- asserted, non-emptily, by
# `tests/test_reporting_registry.py::test_denylisted_columns_are_real_columns`.
# A denylist whose names have drifted denies nothing.
# --------------------------------------------------------------------------- #
_NEVER_A_COLUMN: frozenset[str] = frozenset(
    {
        # Agent certificate material. `agents` IS a readable source (fleet
        # posture reads `status`/`last_seen_at`), which is precisely why the
        # prohibition has to be column-level: the table is mapped to
        # `devices.read`, and a certificate fingerprint is not a `devices.read`
        # fact.
        "agents.cert_fingerprint",
        "agents.cert_serial",
        # §0.9: a chain link outside the chain invites someone to treat a CSV as
        # verifiable, and it is not. The audit API is the artefact.
        "audit_events.hash",
        "audit_events.prev_hash",
        # `jobs` became a mapped source on 2026-08-21 (`patch-compliance`'s
        # `last_scan_at`), and these three are the reason that had to come with
        # a column-level refusal rather than the table mapping alone.
        #
        # `params` IS A LIVE BEARER CREDENTIAL FOR SOME JOB TYPES. The jobs API
        # does not echo it raw: `app/api/v1/jobs.py::_readable_params` runs the
        # job type's `params_redactor` first, because `app.deploy` signs a
        # presigned artifact URL into its params and returning it would hand
        # every `jobs.read` holder a live grant. A report selecting the column
        # would route around that redactor entirely -- the same fact, the same
        # gate, one layer lower.
        #
        # `signature` and `nonce` are the signed-job engine's own material and
        # the jobs API returns NEITHER. A report is not the place they start
        # being readable, and invariant #1 is not a thing to erode by CSV.
        #
        # STILL TRUE AFTER 2026-08-22, when `JobOut` grew signature FIELDS
        # (finding B43). What it grew is `signed`, `signature_algorithm` and a
        # SHA-256 `signature_digest` — proof that a signature exists and a
        # one-way name for it, derived in `app/jobs/attestation.py`. The
        # signature and the nonce themselves are still returned by nothing,
        # which is why both entries stay here rather than following the route.
        "jobs.params",
        "jobs.signature",
        "jobs.nonce",
        # The licence-key ciphertext (`GET /licenses/{id}/key` is gated on
        # `licenses.keys.read`, the catalogue on `licenses.read`). `license` is
        # not in `TABLE_PERMISSIONS` today -- no v1 report reads it -- and this
        # entry is what makes adding it safe rather than a decision someone
        # takes without noticing.
        "license.key_sealed",
        # §5.3, §0.8. `backup_repositories` IS a readable source
        # (`backup-coverage` reads `name`), which is why the prohibition has to
        # be column-level: a repository's storage TARGET and its encryption KEY
        # REFERENCE are infrastructure identifiers, not coverage facts, and a
        # CSV of every org's storage endpoints is reconnaissance whatever the
        # gate on it says.
        "backup_repositories.storage_ref",
        "backup_repositories.encryption_key_ref",
        # The vault columns on `credentials`. `credentials` is on
        # `_NEVER_A_SOURCE` in its entirety, so these are redundant TODAY and
        # deliberately kept: the day someone argues the metadata is harmless and
        # maps the table, the column tail is what still refuses.
        "credentials.ciphertext",
        "credentials.wrapped_dek",
    }
)

def export_path(key: str) -> str:
    """The full path of a definition's export route.

    ONE DEFINITION OF THIS STRING, used by the generation loop in `api.py`, by
    `_NEVER_A_TOOL` below and by the tests -- so a change to the shape of an
    export path cannot leave the denylist pointing at the old one.
    """
    return f"/api/v1/reports/{key}/export.csv"


# --------------------------------------------------------------------------- #
# Reporting's own never-a-tool list (§10.3): `(method, path)` pairs that may
# never become an AI `ReadTool`, whatever the five properties say about them.
#
# EVERY EXPORT ROUTE IS ON IT. An export is a `GET` that performs a write (the
# `report.exported` audit event, §0.7), so it is not method-honest, and
# `PlainTextResponse` fails property 3 anyway.
#
# WRITTEN OUT AS LITERALS RATHER THAN DERIVED FROM `REGISTRY`, and the
# duplication is the point: a list computed from the registry would grow itself
# to cover a new report and could never be wrong, which makes it a comment
# rather than a tripwire. `tests/test_reporting_properties.py` asserts (a) this
# set EQUALS the set of export paths the loop generates -- so a new report whose
# export nobody denylisted is a red test naming it -- and (b) every entry
# resolves to exactly one REAL route on the built app, following
# `test_denylist_entries_resolve_to_real_routes`'s liveness discipline.
# --------------------------------------------------------------------------- #
_NEVER_A_TOOL: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/reports/backup-coverage/export.csv"),
        ("GET", "/api/v1/reports/fleet-posture/export.csv"),
        ("GET", "/api/v1/reports/patch-compliance/export.csv"),
        ("GET", "/api/v1/reports/sla-attainment/export.csv"),
    }
)


@dataclass(frozen=True)
class ReportDefinition:
    """One report: a key, typed params, a typed row model, a declared set of
    source tables, and bounded statements to build (spec §3).

    `requires` IS NOT A CONSTRUCTOR PARAMETER. It is `field(init=False)`,
    computed in `__post_init__` from `sources` through `TABLE_PERMISSIONS`. A
    definition that could hand-write it could understate it, and understating it
    is the exact hole §0.4 exists to close -- so the type system refuses the
    argument rather than a reviewer having to notice it.

    TWO FIELDS DIVERGE FROM §3'S PRINTED DATACLASS, and both are recorded in the
    spec amendment rather than smuggled in:

    * `build_totals` -- §6.3 requires `totals` computed over the WHOLE match and
      never over the page ("a paged total that changes as you page is worse than
      no total"), which is a second statement. §3's printed dataclass has only
      `build`, so totals would otherwise have to be computed in Python over
      every matching row -- which is unbounded, and unbounded is the one thing
      §0.3 buys the statement timeout to avoid. `sources.py` verifies BOTH
      statements against `sources`, so the second one cannot become a
      back-door join.
    * `reproducible` -- §6.3's `meta.reproducible` differs per report (`False`
      for `sla-attainment`, whose sources are mutable by design; `True` for the
      current-state reports), so it is a property of the definition, not a
      constant.
    """

    key: str
    summary: str
    description: str
    params: type[BaseModel]
    row_model: type[BaseModel]
    sources: frozenset[str]
    build: Callable[..., Select[Any]]
    build_totals: Callable[..., Select[Any]]
    max_limit: int
    export_columns: tuple[str, ...]
    export_free_text: frozenset[str]
    export_row_cap: int
    # DERIVED. See the class docstring; `field(init=False)` is the enforcement.
    requires: frozenset[str] = field(init=False)
    reproducible: bool = True

    def __post_init__(self) -> None:
        # THE DENYLIST IS CHECKED FIRST, AND THE ORDER IS OBSERVABLE. Every
        # `_NEVER_A_SOURCE` entry is absent from `TABLE_PERMISSIONS` (asserted),
        # so reversing these two blocks would report a denylisted table as
        # merely *unmapped* -- a different exception type, naming the same
        # table, and a materially weaker statement about why it was refused. A
        # forbidden table declared honestly is still refused.
        for name in sorted(self.sources):
            if name in _NEVER_A_SOURCE:
                raise DenylistedSource(self.key, name)
        missing = sorted(t for t in self.sources if t not in TABLE_PERMISSIONS)
        if missing:
            raise UndeclaredSource(
                self.key,
                missing,
                reason=(
                    "declares source(s) absent from TABLE_PERMISSIONS, so no read "
                    "permission governs them"
                ),
            )
        object.__setattr__(
            self, "requires", frozenset(TABLE_PERMISSIONS[t] for t in self.sources)
        )

    def params_json_schema(self) -> dict[str, Any]:
        """The JSON Schema of this report's query parameters (§17.11).

        **DERIVED FROM `self.params`, WHICH IS THE OBJECT THE ROUTE VALIDATES
        WITH**, for exactly the reason `requires` is derived from `sources`:
        a second, hand-written copy of a params contract is a second contract,
        and two contracts drift in whichever direction suits the person editing
        one of them. `api.py` binds `definition.params` as the route's query
        model (`Annotated[params_model, Query()]`); this method reads the same
        attribute. There is no `params_schema=` constructor argument, so a
        definition cannot publish a shape it does not enforce -- the same
        `field(init=False)` discipline, applied by having no field at all.

        **THIS IS A DESCRIPTION OF THE GATE, NEVER A REPLACEMENT FOR IT.**
        Publishing it widens nothing: the model still rejects, `extra="forbid"`
        still makes an unknown parameter a 422, and every `ge`/`le` still binds.
        A caller who reads the schema learns what the server would have told
        them with a 422 anyway, one round trip sooner.

        `by_alias=True` IS PYDANTIC'S DEFAULT AND IS LOAD-BEARING HERE.
        `sla-attainment`'s window is `window_from`/`window_to` in Python and
        `from`/`to` on the wire; a schema naming the Python attribute would tell
        a client to send a parameter `extra="forbid"` rejects, which is worse
        than publishing nothing.

        A FRESH DICT PER CALL, deliberately: the returned mapping is handed
        into a response model and nothing stops a caller mutating it, so it must
        not be a shared module-level object that a mutation would poison for
        every subsequent request.
        """
        return self.params.model_json_schema()


_REGISTRY: dict[str, ReportDefinition] = {}

# A `MappingProxyType`, not a dict (§3): "a mapping any code in the process can
# widen at runtime is not compiled into the image". The proxy is a live view of
# `_REGISTRY`, so `register()` below is still the one way in -- and it is the
# only thing holding a writable reference.
REGISTRY: Mapping[str, ReportDefinition] = MappingProxyType(_REGISTRY)


def register(definition: ReportDefinition) -> None:
    """Add a definition to the catalogue. Called ONLY by
    `definitions/__init__.py` at import time -- there is no runtime registration
    path, by design (§0.1)."""
    if definition.key in _REGISTRY:
        raise ValueError(  # i18n-exempt: build-time, aborts the import and is never shown
            f"duplicate report key {definition.key!r}"
        )
    if not definition.key or "/" in definition.key:
        # §6.1 generates a literal path per definition; a key containing a
        # slash would mint a path segment nobody declared, and an empty key
        # would collide with the catalogue route itself.
        raise ValueError(  # i18n-exempt: build-time, aborts the import and is never shown
            f"report key {definition.key!r} is not URL-safe"
        )
    _REGISTRY[definition.key] = definition
