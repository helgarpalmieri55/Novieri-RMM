"""`frontend/src/modules/patch/types.ts` must match the patch API's own models.

WHY THIS IS A TEST AND NOT A ONE-TIME FIX. The header of `types.ts` says
"Mirrors backend/app/modules/patch/api.py response/request models exactly", and
it did not: `DeviceStatusOut.last_scan_at` and `FleetReportOut`'s
`last_scan_at` / `devices_scanned` / `devices_total` had been in the responses
since 2026-08-21 and in no interface. Nothing was broken by that -- TypeScript
cannot know a field it was never told about -- which is exactly the problem. A
field the console cannot see is a fact the console cannot show, and the failure
mode is a page that renders a confident zero where the server sent a "nobody has
measured this yet". That is the defect this module has now filed twice.

WHAT IT COMPARES, AND WHAT IT DOES NOT. Field NAMES, per model, both ways. Not
types: a `datetime` is a `string` over the wire and a `UUID` is a `string` too,
so a type comparison would either be a table of aliases nobody maintains or a
guard that fails on correct code. Names are where drift actually happens --
a field added to a response and never told to the console, or an interface
keeping a field the API stopped sending.

IT ALSO REQUIRES THE MAPPING TO BE COMPLETE, in both directions, which is the
half that catches the NEXT one: a new Pydantic response model with no interface
fails here rather than being noticed when a page needs it.

NON-VACUITY. The TypeScript side is parsed with a regex, and a regex that
stops matching is a guard that passes on everything. So every interface is
required to yield at least one field, and the parser is required to find every
interface the mapping names -- a rename or a reformat that defeats the parser
goes red rather than green.
"""

import inspect
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.modules.patch import api as patch_api

TYPES_TS = Path(__file__).resolve().parents[2] / "frontend/src/modules/patch/types.ts"

#: Pydantic model in `app/modules/patch/api.py` -> exported interface in
#: `types.ts`. Every model in that module must appear here; see
#: `test_every_pydantic_model_has_an_interface`.
MODEL_TO_INTERFACE = {
    "PatchOut": "Patch",
    "PatchListResponse": "PatchListResponse",
    "DeviceStatusOut": "DeviceStatus",
    "ScanRequest": "ScanRequest",
    "ScanResponse": "ScanResponse",
    "ApprovalCreateRequest": "ApprovalCreateRequest",
    "BulkApprovalCreateRequest": "BulkApprovalCreateRequest",
    "ApprovalOut": "PatchApproval",
    "ApprovalListResponse": "ApprovalListResponse",
    "BulkApprovalResponse": "BulkApprovalResponse",
    "RingClientCountOut": "PatchRingClientCount",
    "RingOut": "PatchRing",
    "RingListResponse": "PatchRingListResponse",
    "UpdateRollupOut": "PatchUpdate",
    "UpdateRollupListResponse": "PatchUpdateListResponse",
    "DeviceRollupOut": "PatchDeviceRollup",
    "DeviceRollupListResponse": "PatchDeviceRollupListResponse",
    "PatchRunFailureOut": "PatchRunFailure",
    "PatchRunOut": "PatchRun",
    "PatchRunListResponse": "PatchRunListResponse",
    "ApplyRequest": "ApplyRequest",
    "ApplyResponse": "ApplyResponse",
    "PolicyOut": "PatchPolicy",
    "PolicyUpsertRequest": "PatchPolicyInput",
    "ExposureOut": "Exposure",
    "FleetReportOut": "FleetReport",
    "RestartOut": "PatchRestart",
    "RestartListItem": "PatchRestartListItem",
    "RestartListResponse": "PatchRestartListResponse",
    "RestartWindowOut": "PatchRestartWindow",
    "RestartRequestOut": "PatchRestartRequestResponse",
    "AppSourceOut": "PatchAppSource",
    "AppSourceSummaryOut": "PatchAppSourceSummary",
    "AppSourceSummaryResponse": "PatchAppSourceSummaryResponse",
}

#: Interfaces that describe something OTHER than a response or request body --
#: the query strings the client builds, and the console's own view state. They
#: have no Pydantic counterpart because FastAPI expresses query parameters as
#: function arguments rather than as a model.
TS_ONLY_INTERFACES = frozenset(
    {
        "ApprovalListFilters",
        "PatchUpdateFilters",
        "PatchDeviceRollupFilters",
        "PatchRunFilters",
    }
)

_INTERFACE = re.compile(r"^export interface (\w+) \{$(.*?)^\}$", re.MULTILINE | re.DOTALL)
_FIELD = re.compile(r"^  (\w+)\??:", re.MULTILINE)


def _interfaces() -> dict[str, set[str]]:
    """Every `export interface` in `types.ts`, and the field names in it.

    Deliberately crude, and deliberately strict about shape: fields are matched
    at exactly one level of indentation, so a nested object literal's members
    are not mistaken for the interface's own. Every interface in that file is
    flat today, and one that stops being flat should be looked at rather than
    silently half-parsed.
    """
    source = TYPES_TS.read_text()
    return {
        name: set(_FIELD.findall(body)) for name, body in _INTERFACE.findall(source)
    }


def _models() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in inspect.getmembers(patch_api, inspect.isclass)
        if issubclass(obj, BaseModel)
        and obj is not BaseModel
        and obj.__module__ == patch_api.__name__
    }


def test_the_parser_can_still_see_the_file() -> None:
    """THE ANTI-VACUITY CHECK, and it runs first. Every assertion below is of
    the form "these two sets are equal"; a parser that returned nothing would
    make most of them trivially true, and the mapping check would fail with a
    message about missing interfaces rather than about a broken parser."""
    interfaces = _interfaces()
    assert TYPES_TS.exists(), TYPES_TS
    assert len(interfaces) >= 15, f"only parsed {len(interfaces)} interfaces out of {TYPES_TS}"
    for name, fields in interfaces.items():
        assert fields, f"interface {name} parsed with no fields at all"
    assert "identifier" in interfaces["Patch"], "the parser is not reading field names"


def test_every_pydantic_model_has_an_interface() -> None:
    """THE HALF THAT CATCHES THE NEXT DRIFT. A response model added to the
    patch API with nothing on the console side fails here, at the moment it is
    added, rather than the next time somebody needs the field it carries."""
    unmapped = sorted(set(_models()) - set(MODEL_TO_INTERFACE))
    assert not unmapped, (
        f"{unmapped} are Pydantic models in app/modules/patch/api.py with no entry in "
        "MODEL_TO_INTERFACE. Add the TypeScript interface to "
        "frontend/src/modules/patch/types.ts and map it here."
    )
    stale = sorted(set(MODEL_TO_INTERFACE) - set(_models()))
    assert not stale, f"{stale} are mapped here and no longer exist in the API module"


def test_every_interface_is_a_model_or_declared_ts_only() -> None:
    interfaces = set(_interfaces())
    mapped = set(MODEL_TO_INTERFACE.values()) | TS_ONLY_INTERFACES
    orphans = sorted(interfaces - mapped)
    assert not orphans, (
        f"{orphans} are exported from types.ts and describe no patch API model. Either map "
        "them in MODEL_TO_INTERFACE or, if they are query parameters or view state, add them "
        "to TS_ONLY_INTERFACES."
    )
    missing = sorted(mapped - interfaces - TS_ONLY_INTERFACES)
    assert not missing, f"{missing} are mapped here and absent from types.ts"


@pytest.mark.parametrize(("model_name", "interface_name"), sorted(MODEL_TO_INTERFACE.items()))
def test_model_and_interface_carry_the_same_fields(
    model_name: str, interface_name: str
) -> None:
    model = _models()[model_name]
    interface = _interfaces().get(interface_name)
    assert interface is not None, f"types.ts has no `export interface {interface_name}`"
    expected = set(model.model_fields)
    assert interface == expected, (
        f"{model_name} <-> {interface_name}: "
        f"the API sends {sorted(expected - interface)} that types.ts does not declare, and "
        f"types.ts declares {sorted(interface - expected)} that the API does not send."
    )
