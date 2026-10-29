"""The response envelope (spec §6.3).

ONE GENERIC ENVELOPE, NOT A HAND-WRITTEN PAGE MODEL PER REPORT: one place where
the meta fields are described, and one place a described field can be added.
Property 5 of §0.3's "clean enough to be a tool" is that every field carries a
`Field(description=...)` -- not a class-docstring fallback -- and measured
against this repo at HEAD there was exactly ONE `Field(description=...)` in any
module's `schema.py`. A per-report envelope would put that obligation on every
future report author; a generic one discharges it once, here.

`totals` ARE COMPUTED OVER THE WHOLE MATCH, NEVER OVER THE PAGE. A paged total
that changes as you page is worse than no total: it is a number a reader will
quote. `tests/test_reporting_fleet_posture.py` pins it across two offsets on
rows whose values differ, so a totals accidentally computed over the page
produces a DIFFERENT number rather than the same one by coincidence.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReportSource(BaseModel):
    """One module a report read, and the recency of its data."""

    module: str = Field(description="The module whose tables this figure came from.")
    permission: str = Field(
        description=(
            "The read permission that governs it. The caller holds every one of these "
            "or the report is a 403 -- reporting shows nothing the module APIs would "
            "refuse."
        )
    )
    recency: str = Field(
        description=(
            "What the timestamps in this source mean, e.g. 'as of each device's last "
            "patch scan'. A figure is only as fresh as the record it was read from, "
            "and this says which."
        )
    )


class ReportMeta(BaseModel):
    """How, when and from what a page was built."""

    report_key: str = Field(description="Stable identifier of the report definition.")
    generated_at: datetime = Field(
        description=(
            "UTC instant the query ran. Rows reflect the record at this moment and at "
            "no other -- reports are computed live, not from a snapshot."
        )
    )
    reproducible: bool = Field(
        description=(
            "False when a source is mutable, so re-running this report for the same "
            "window may return different numbers. Quote a figure from a "
            "non-reproducible report and you are quoting today's record of that "
            "period, not the period."
        )
    )
    window_from: datetime | None = Field(
        description="Inclusive start of the window, or null for a point-in-time report."
    )
    window_to: datetime | None = Field(
        description="Exclusive end of the window, or null for a point-in-time report."
    )
    total: int = Field(description="Total matching rows before limit/offset.")
    limit: int = Field(description="Maximum rows returned by this request.")
    offset: int = Field(description="Rows skipped before the first returned row.")
    sources: list[ReportSource] = Field(
        description="Every module this report read, with the recency of its data."
    )


class CatalogueEntry(BaseModel):
    """One report as it looks TO THE CALLER ASKING (spec §6.2, §4.1).

    `entitled` and `missing_permissions` are computed against the requester's own
    grants, which is the entire reason this route exists: `reporting.read` grants
    nothing on its own (§4.3), so without a catalogue an operator's first
    encounter with the module is an opaque 403 naming a code they have never
    heard of. Nothing is hidden -- a report you cannot run is a permissions
    conversation, not a secret (§11).
    """

    key: str = Field(description="Stable identifier, and the last path segment of its route.")
    summary: str = Field(description="One line: what question this report answers.")
    description: str = Field(
        description=(
            "What each row means, what is deliberately excluded, and how to read the "
            "numbers. The same prose the route's OpenAPI description carries."
        )
    )
    params_schema: dict[str, Any] = Field(
        description=(
            "JSON Schema of this report's query parameters, DERIVED from the same "
            "Pydantic model the route validates against -- so it DESCRIBES the gate and "
            "never replaces it. Property names are WIRE names (`from`, not "
            "`window_from`); `required` lists the parameters that have no default; "
            "`additionalProperties: false` is the model's `extra=\"forbid\"`, so a "
            "parameter absent from `properties` is a 422 rather than a filter that is "
            "silently ignored. Bind one control per property instead of hardcoding a "
            "params bar -- a client that hardcodes it is a second copy of this contract. "
            "PLATFORM DATA: byte-identical for every caller in every org, and NOT a "
            "statement about what you may run -- `entitled` is that."
        )
    )
    requires: list[str] = Field(
        description=(
            "Every module read permission this report needs, sorted. DERIVED from the "
            "tables it reads, not declared -- so it can never understate what the report "
            "would show you."
        )
    )
    entitled: bool = Field(
        description=(
            "True when at least one client holds `reporting.read` and every code in "
            "`requires` for you: the report runs over exactly those clients. False means "
            "it answers 403 for you, or runs over no client, and `missing_permissions` "
            "says what to ask for."
        )
    )
    missing_permissions: list[str] = Field(
        description=(
            "The codes in `requires` you hold on no client, sorted -- or, when you hold "
            "each of them but on no one client together, the ones missing from a client "
            "where you hold `reporting.read`. Empty exactly when `entitled` is true."
        )
    )


class ReportCatalogue(BaseModel):
    """Every report this deployment can run. Platform data only: the catalogue
    is compiled into the image (§0.1), so it exposes nothing about any org."""

    reports: list[CatalogueEntry] = Field(
        description=(
            "One entry per report definition, sorted by key. EVERY report is listed, "
            "including the ones you may not run -- a catalogue that hid those would "
            "give an under-permissioned operator the same answer as a smaller "
            "deployment, with no way to tell them apart."
        )
    )


class ReportPage[RowT: BaseModel](BaseModel):
    """One page of one report."""

    meta: ReportMeta = Field(description="How, when and from what this page was built.")
    totals: dict[str, int] = Field(
        description=(
            "Report-level aggregates over ALL matching rows, not just this page. These "
            "do not change as you page."
        )
    )
    rows: list[RowT] = Field(
        description=(
            "One row per subject, ordered stably and totally, so paging by offset can "
            "neither skip nor duplicate a row between requests."
        )
    )
