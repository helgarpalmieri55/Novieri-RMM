# Patch fleet view — rollup first, detail on drill-down

Status: built (2026-08-25). Operator report, 2026-08-25: *"patch management is still a
super long list of missing updates and that's just one device online, that view
needs to change"*. Amends `spec.md` §12 (API) and §14 (frontend); no change to
§4 (data model), §7 (approvals), or §15 (invariants).

## 1. What is actually wrong

`/patch` renders, in order: 4 tiles, two compliant-% panels, an **Exposed
devices** table, an Approvals table, and a **Patch catalog** table. Two of those
are the defect:

* **Exposed devices is one row per `(device, patch)` pair.** It is
  `fleet_report()["exposures"]`, uncapped, un-paginated, every non-compliant
  status row in the org. One Ubuntu box with 300 upgradable packages is 300
  rows.
* **Patch catalog is one row per patch, capped at 500, and carries no fleet
  count.** The same 300 packages again, and no way to tell an update missing on
  one machine from one missing on forty — `GET /patches` returns bare `patches`
  rows.

So a one-device fleet renders ~600 rows to say one thing, and the number that
would make the list actionable ("N of your M devices are missing this") is not
in any response. Filtering and searching happen in the browser over the fetched
page, so they cannot reach past the cap either.

## 2. The shape

**The primary object of the fleet view is the PATCH, aggregated across
devices.** Approving is already "a decision about a patch and a ring, not about
a device" — `patch_approvals` is `(org_id, patch_id, ring_name)` with no device
column — so the list an operator works down should be keyed the same way.
Per-device detail is a drill-down, not the default.

| Panel | Now | After |
|---|---|---|
| Stat tiles | 4, counted in the browser from a capped page | 4, every figure read off the same aggregate the table below is paged from |
| Compliant % by OS / by severity | two panels | unchanged, moved below the working list |
| **Exposed devices** (pair-per-row) | one row per (device, patch) | **deleted.** Replaced by **Devices needing patches**: one row per device — hostname, N outstanding, worst severity, last scan — expandable to that device's own updates |
| Approvals | table + revoke | unchanged |
| **Patch catalog** (browser-filtered, 500 cap) | one row per patch, no counts | **Updates**: one row per patch, **server-paged/sorted/filtered**, carrying `devices_missing`, the rings it is already cleared for, and the bulk-approve selection |

Row counts, one Ubuntu device with 300 outstanding packages, 56 of them from
the security pocket. **Measured in Chromium at 1440px**, the page rendered
against a canned API of exactly that fleet:

| | `tbody tr` | `scrollHeight` |
|---|---|---|
| before | **601** | **29,317px** |
| after | **26** | **2,740px** |

26 is 25 updates on page 1 plus the one device row, with `total` stating 300
and 1 in words above each table. axe (wcag2a/2aa/21a/21aa/22aa) reports no
violations at 1440, 1024 and 640px.

### Severity default

The fleet **Updates** list defaults to `critical` + `important` — the severities
that demand a decision — with:

* the filter visible in the filter bar and in the URL (`?severity=critical,important`),
* a permanent line above the table reading *"Showing 1–8 of 8 updates. 292 more
  are outside the severity filter — show all 300."*, so a filtered count can
  never be misread as a total,
* one click to widen, and the CSV export unchanged and complete.

**The per-device panel is never severity-filtered.** A device row always states
its *total* outstanding count, so nothing a device is missing can be invisible
from the fleet page — only deprioritised in the update list.

**Reversible, and here is the risk.** `agent/internal/capabilities/patch/
winupdate.go` maps an unrated `IUpdate.MsrcSeverity` to `unknown`, and its own
comment says *"most cumulative updates for Windows 11 carry no MSRC severity at
all despite containing security fixes"*. So on Windows this default can hide the
monthly rollup. It is well aimed at the platform that actually produces the long
list — `apt.go` marks security-pocket versions `important` and everything else
`unknown`, which is exactly the 300-row case — but on a Windows-only fleet the
right default may be "all". It is one constant, `DEFAULT_SEVERITIES` in
`frontend/src/modules/patch/UpdatesPanel.tsx`.

## 3. The new endpoints

Both `patch.read`, both inside `org_session`, both with counts computed in SQL
(a browser that fetched 500 rows and grouped them would be the same defect one
layer down), both paginated.

### `GET /api/v1/patch/updates`

One row per patch, aggregated over `device_patch_status`.

```jsonc
{
  "items": [{
    "id": "...", "os_family": "linux", "source": "apt",
    "identifier": "openssl@3.0.13", "title": "…", "severity": "important",
    "kb": null, "cve": ["CVE-…"], "priority": null, "first_seen_at": "…",
    "devices_missing": 41,        // state = missing
    "devices_approved": 6,        // cleared, awaiting a window
    "devices_failed": 1,
    "devices_outstanding": 48,    // the three above; the number that makes the row actionable
    "devices_installed": 12,
    "cleared_rings": ["test", "pilot"]   // un-revoked approvals, rollout order
  }],
  "total": 8,                     // matches ALL filters, before limit/offset
  "total_unfiltered": 300,        // same filters MINUS severity
  "severity_counts": {"critical": 2, "important": 6, "unknown": 292},
  "last_scan_at": "…", "devices_scanned": 1, "devices_total": 1
}
```

Query: `severity` (repeatable), `os_family`, `source`, `ring`, `device_id`,
`q` (identifier/title, case-insensitive), `outstanding_only` (default `true`),
`sort` (`severity` default | `devices` | `identifier` | `first_seen`),
`limit` (default 25, max 200), `offset`.

Three things are deliberate:

* **`total_unfiltered` and `severity_counts` come back with the page**, because
  the tiles above the table must be the same arithmetic as the table. The last
  cycle produced a tile/table disagreement twice; a tile counted in the browser
  from a capped page is how.
* **`severity_counts` applies every filter EXCEPT severity**, so the chips and
  the "292 more" line are true *in the current context*, and
  `sum(severity_counts) == total_unfiltered` by construction.
* **`device_id`** makes this one route serve three surfaces: the fleet list, a
  device row's expansion, and `DevicePatchTab`. A per-device patch list is the
  same query with one more predicate.

A distinct-device count is deliberately NOT on this response. It belongs to the
device rollup below, and two numbers a page could describe as "devices needing
patches" is how a tile and the table under it come to disagree.

`cleared_rings` is on the row rather than joined in the browser: the console
previously derived it from a capped `GET /approvals` page and had to print a
disclaimer that the column under-reports.

### `GET /api/v1/patch/device-rollup`

One row per device that has at least one outstanding patch.

```jsonc
{
  "items": [{
    "device_id": "…", "hostname": "wks-01", "os_family": "windows",
    "outstanding": 300, "counts_by_severity": {"critical": 0, "important": 8, …},
    "worst_severity": "important", "last_scan_at": "…"
  }],
  "total": 1,             // devices with something outstanding
  "devices_total": 1, "devices_scanned": 1, "last_scan_at": "…"
}
```

Query: `q` (hostname), `os_family`, `limit` (default 25, max 200), `offset`.
Ordered worst-first: severity rank of the worst outstanding patch, then
`outstanding` desc, then hostname.

`total` vs `devices_total` is the fix for the 2026-08-20 defect in its other
direction: "1 device needs patches" is only readable beside "of 1 device".

## 4. Frontend split

`FleetPatchPage.tsx` is 1076 lines. Split by responsibility:

```
FleetPatchPage.tsx     page shell: head, tiles, panel composition
UpdatesPanel.tsx       the fleet update list: filters, paging, bulk approve
DevicesPanel.tsx       device rollup + per-device expansion
ApprovalsPanel.tsx     approvals table + revoke confirmation
CompliancePanels.tsx   compliant % by OS family / by severity
patchCopy.ts           pure sentence builders (scan queued, batch outcome, counts)
```

`DevicePatchTab.tsx` gains the device's own outstanding update list
(`/patch/updates?device_id=…`) beneath its three count cards — the (device,
patch) detail belongs there, not on the fleet page.

## 5. Type drift

`frontend/src/modules/patch/types.ts` is missing `last_scan_at`,
`devices_scanned` and `devices_total` — fields `FleetReportOut` and
`DeviceStatusOut` have returned since 2026-08-21. Reconciling them is a
one-time fix; the drift recurring is the actual problem, so
`backend/tests/test_patch_type_parity.py` introspects the patch API's Pydantic
response models, parses the exported interfaces out of `types.ts`, and requires
the field-name sets to match. It carries its own non-vacuity check: an
extractor that stops finding fields fails rather than passing.
