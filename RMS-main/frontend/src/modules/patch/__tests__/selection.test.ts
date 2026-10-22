import { describe, expect, it, vi } from "vitest";

import {
  APPLY_HOST_MAX,
  BULK_APPROVAL_MAX,
  HOST_FETCH_PAGE,
  UPDATE_FETCH_PAGE,
  resolveMatchingHostIds,
  resolveMatchingUpdateIds,
} from "../selection";
import {
  applyOutcomeMessage,
  hostLensWarning,
  machinesNeedingUpdates,
  selectAllMatchingLabel,
  selectionCeilingNotice,
  severitiesFromParam,
  tabEmptyReason,
} from "../patchCopy";
import { defaultSeveritiesFor, patchFamilyFromParam } from "../patchTabs";
import type {
  PatchDeviceRollupFilters,
  PatchDeviceRollupListResponse,
  PatchUpdate,
  PatchUpdateFilters,
  PatchUpdateListResponse,
} from "../types";

/**
 * THE OPERATOR'S SECOND COMPLAINT, 2026-08-27: "i have to go back and approve
 * 25 updates per page, that's not ideal".
 *
 * These tests are about the thing that fixes it WITHOUT giving up the property
 * `BulkApprovalCreateRequest` was designed around. Its docstring: "A filter
 * ('approve everything critical') is evaluated on the server AFTER the
 * operator has read the screen, so the set that gets cleared is not the set
 * they looked at; a list is exactly what they ticked." So the filter is
 * resolved to an explicit id list in the browser, at the moment the operator
 * asks, and the count is on screen before anything is sent.
 */

function row(id: string): PatchUpdate {
  return {
    id,
    os_family: "linux",
    source: "apt",
    identifier: `pkg-${id}`,
    title: `package ${id}`,
    severity: "important",
    kb: null,
    cve: null,
    priority: null,
    first_seen_at: "2026-08-01T00:00:00.000Z",
    devices_missing: 1,
    devices_approved: 0,
    devices_failed: 0,
    devices_installed: 0,
    devices_outstanding: 1,
    device_state: null,
    device_last_error: null,
    device_applied_at: null,
    cleared_rings: [],
  };
}

/** A server holding `total` rows, serving whatever window is asked for. */
function serverOf(total: number) {
  return vi.fn(async (params: PatchUpdateFilters): Promise<PatchUpdateListResponse> => {
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 25;
    const items: PatchUpdate[] = [];
    for (let index = offset; index < Math.min(total, offset + limit); index += 1) {
      items.push(row(`p${index}`));
    }
    return {
      items,
      total,
      total_unfiltered: total,
      severity_counts: { important: total },
      last_scan_at: "2026-08-26T00:00:00.000Z",
      devices_scanned: 1,
      devices_total: 1,
    };
  });
}

describe("resolveMatchingUpdateIds", () => {
  it("returns every id the criterion matches, walking past one page of it", async () => {
    const listUpdates = serverOf(300);

    const resolved = await resolveMatchingUpdateIds(listUpdates, { severity: ["important"] });

    expect(resolved.ids).toHaveLength(300);
    expect(resolved.matching).toBe(300);
    expect(resolved.truncated).toBe(false);
    // Two requests, not twelve: the walk uses the route's own ceiling, because
    // nobody reads these rows -- they are being counted.
    expect(listUpdates).toHaveBeenCalledTimes(Math.ceil(300 / UPDATE_FETCH_PAGE));
    expect(listUpdates).toHaveBeenLastCalledWith(
      expect.objectContaining({ severity: ["important"], limit: UPDATE_FETCH_PAGE, offset: 200 }),
    );
  });

  it("resolves the WHOLE criterion, not the page the operator happens to be on", async () => {
    const listUpdates = serverOf(60);

    // The caller is on page 2. "Select all matching" means all of them.
    const resolved = await resolveMatchingUpdateIds(listUpdates, { offset: 25, limit: 25 });

    expect(resolved.ids[0]).toBe("p0");
    expect(resolved.ids).toHaveLength(60);
    expect(listUpdates).toHaveBeenCalledWith(expect.objectContaining({ offset: 0 }));
  });

  it("carries the criterion into every request, so no page comes from a different filter", async () => {
    const listUpdates = serverOf(400);

    await resolveMatchingUpdateIds(listUpdates, { q: "openssl", os_family: "linux", severity: [] });

    for (const call of listUpdates.mock.calls) {
      expect(call[0]).toMatchObject({ q: "openssl", os_family: "linux", severity: [] });
    }
  });

  it("stops at the ceiling and SAYS it stopped, rather than approving a prefix quietly", async () => {
    const listUpdates = serverOf(812);

    const resolved = await resolveMatchingUpdateIds(listUpdates, {});

    expect(resolved.ids).toHaveLength(BULK_APPROVAL_MAX);
    expect(resolved.matching).toBe(812);
    expect(resolved.truncated).toBe(true);
    // ...and the console has the words for it before the click, not after a 422.
    expect(selectAllMatchingLabel(812, BULK_APPROVAL_MAX)).toBe("Select the first 500 of 812");
    expect(selectionCeilingNotice(812, BULK_APPROVAL_MAX)).toContain("remaining 312");
  });

  it("says nothing about a ceiling the criterion does not reach", async () => {
    const listUpdates = serverOf(7);

    const resolved = await resolveMatchingUpdateIds(listUpdates, {});

    expect(resolved.truncated).toBe(false);
    expect(selectAllMatchingLabel(7, BULK_APPROVAL_MAX)).toBe("Select all 7 updates");
    expect(selectionCeilingNotice(7, BULK_APPROVAL_MAX)).toBe("");
  });

  it("does not spin when the server's total disagrees with what it serves", async () => {
    // A scan completing mid-walk moves rows across page boundaries. A resolver
    // that trusted `total` and looped until it had that many ids would never
    // return; this one stops on an empty page.
    const listUpdates = vi.fn(async (params: PatchUpdateFilters) => ({
      items: (params.offset ?? 0) === 0 ? [row("p0")] : [],
      total: 500,
      total_unfiltered: 500,
      severity_counts: {},
      last_scan_at: null,
      devices_scanned: 0,
      devices_total: 0,
    }));

    const resolved = await resolveMatchingUpdateIds(listUpdates, {});

    expect(resolved.ids).toEqual(["p0"]);
    expect(listUpdates.mock.calls.length).toBeLessThan(5);
  });

  it("counts a patch once when a concurrent write hands it back on two pages", async () => {
    const listUpdates = vi.fn(async (params: PatchUpdateFilters) => ({
      items: (params.offset ?? 0) === 0 ? [row("p0"), row("p1")] : [row("p1"), row("p2")],
      total: 4,
      total_unfiltered: 4,
      severity_counts: {},
      last_scan_at: null,
      devices_scanned: 1,
      devices_total: 1,
    }));

    const resolved = await resolveMatchingUpdateIds(listUpdates, {});

    // The count beside the button and the count in the request are the same
    // number, which is the whole property this module keeps losing.
    expect(new Set(resolved.ids).size).toBe(resolved.ids.length);
  });
});

// ===========================================================================
// THE SAME MOVE OVER MACHINES — the Linux tab's unit of decision.
//
// The operator, 2026-08-26: "Unit of decision: the machine ... apt/dnf resolve
// dependencies as a SET — cherry-picking single packages often isn't
// actionable anyway." So the Linux selection walks `GET /patch/device-rollup`
// and `POST /apply` carries `device_ids` — an explicit list, for exactly the
// reason `patch_ids` is one: the set that acts is the set the operator saw.
// ===========================================================================

describe("resolveMatchingHostIds", () => {
  function pagedHosts(total: number) {
    return vi.fn((params: PatchDeviceRollupFilters) => {
      const offset = params.offset ?? 0;
      const limit = params.limit ?? 25;
      const items = [];
      for (let index = offset; index < Math.min(total, offset + limit); index += 1) {
        items.push({
          device_id: `d${index}`,
          hostname: `host-${index}`,
          os_family: "linux",
          outstanding: total - index,
          failed: 0,
          worst_severity: "important",
          counts_by_severity: {},
          last_scan_at: null,
        });
      }
      return Promise.resolve({
        items,
        total,
        last_scan_at: null,
        devices_scanned: total,
        devices_total: total,
      } satisfies PatchDeviceRollupListResponse);
    });
  }

  it("returns the machines the criterion matches, with the names the dialog prints", async () => {
    const listHosts = pagedHosts(3);
    const resolved = await resolveMatchingHostIds(listHosts, { os_family: "linux" });

    expect(resolved.matching).toBe(3);
    expect(resolved.truncated).toBe(false);
    // BOTH HALVES. The id is what `POST /apply` takes; the hostname is what the
    // confirmation has to be able to say, and re-deriving it from a page the
    // operator may have paged away from is how a dialog names the wrong machine.
    expect(resolved.hosts).toEqual([
      { id: "d0", hostname: "host-0" },
      { id: "d1", hostname: "host-1" },
      { id: "d2", hostname: "host-2" },
    ]);
  });

  it("ignores the caller's paging, because paging is a property of the table", async () => {
    const listHosts = pagedHosts(5);
    await resolveMatchingHostIds(listHosts, { os_family: "linux", offset: 40, limit: 25 });

    expect(listHosts).toHaveBeenCalledWith(
      expect.objectContaining({ offset: 0, limit: HOST_FETCH_PAGE }),
    );
  });

  it("stops at the console's own ceiling, and says the list is a prefix", async () => {
    // `APPLY_HOST_MAX` is NOT a server limit — `ApplyRequest.device_ids` has no
    // bound and `dispatch_apply` will sign a job for every device it is handed.
    // It is the size of a decision: an install is code running as root on other
    // people's machines, and "select all" reaching past two hundred of them
    // from a list nobody scrolled is not a decision anybody made.
    const resolved = await resolveMatchingHostIds(pagedHosts(640), { os_family: "linux" });

    expect(resolved.hosts).toHaveLength(APPLY_HOST_MAX);
    expect(resolved.matching).toBe(640);
    expect(resolved.truncated).toBe(true);
    // WORST-FIRST, which is what makes a prefix defensible: the server orders
    // by severity rank then outstanding count, so the two hundred taken are the
    // two hundred most worth installing on. A prefix of an alphabetical list
    // would be every host beginning with "a".
    expect(resolved.hosts[0]).toEqual({ id: "d0", hostname: "host-0" });
  });

  it("ends the walk on a short page rather than trusting a total it can see is wrong", async () => {
    const listHosts = vi
      .fn()
      .mockResolvedValueOnce({
        items: [
          {
            device_id: "d0",
            hostname: "host-0",
            os_family: "linux",
            outstanding: 1, failed: 0,
            worst_severity: null,
            counts_by_severity: {},
            last_scan_at: null,
          },
        ],
        total: 900,
        last_scan_at: null,
        devices_scanned: 1,
        devices_total: 1,
      })
      .mockResolvedValue({
        items: [],
        total: 900,
        last_scan_at: null,
        devices_scanned: 1,
        devices_total: 1,
      });

    const resolved = await resolveMatchingHostIds(listHosts, { os_family: "linux" });
    expect(resolved.hosts).toHaveLength(1);
    expect(listHosts).toHaveBeenCalledTimes(2);
  });
});

// ===========================================================================
// THE SENTENCES THE LINUX PATH IS ALLOWED TO SAY.
// ===========================================================================

describe("applyOutcomeMessage", () => {
  it("counts jobs, not hosts, because a shut window produces neither a job nor an error", () => {
    expect(applyOutcomeMessage(["j1"], 1)).toBe("Install queued on 1 host.");
  });

  it("names the hosts that got nothing, and both reasons it could be", () => {
    // `ApplyResponse` carries `job_ids` and nothing else, so a host skipped for
    // a shut window and one with nothing approved come back identically.
    // Guessing which would invent a fact; naming both is the true statement.
    const message = applyOutcomeMessage(["j1", "j2"], 5);
    expect(message).toMatch(/Install queued on 2 hosts\./);
    expect(message).toMatch(/3 hosts were skipped/);
    expect(message).toMatch(/nothing on them is approved/);
    expect(message).toMatch(/maintenance window is shut/);
  });

  it("says plainly that nothing installed when nothing did", () => {
    // A 200 with an empty `job_ids` is the ordinary outcome outside a
    // maintenance window. Reporting it as success is how an operator comes to
    // believe a fleet was patched.
    expect(applyOutcomeMessage([], 4)).toMatch(
      /^Nothing was installed\. None of the 4 selected hosts/,
    );
  });
});

describe("tabEmptyReason", () => {
  // Three empty tables, three opposite meanings. `CompliancePanels` painted a
  // row of 0% bars on a fleet nothing had ever scanned, and the dashboard put
  // nine confident zeros on an unmeasured page; this is the discrimination
  // those two were missing, as a pure function so the page only chooses words.
  it("blames enrolment first, because a platform with no machines cannot be scanned", () => {
    expect(tabEmptyReason({ enrolled: 0, devicesScanned: 0 })).toBe("unenrolled");
  });

  it("says nothing was measured when machines exist and no scan has finished", () => {
    expect(tabEmptyReason({ enrolled: 12, devicesScanned: 0 })).toBe("unscanned");
  });

  it("only calls a fleet compliant when it has been counted AND measured", () => {
    expect(tabEmptyReason({ enrolled: 12, devicesScanned: 12 })).toBe("compliant");
  });

  it("refuses to guess when the console may not count the fleet", () => {
    // `devices.read` absent. "No macOS machines" and "all macOS machines are
    // current" are the same empty table, and picking the nicer reading is the
    // failure mode this whole function exists to prevent.
    expect(tabEmptyReason({ enrolled: null, devicesScanned: 12 })).toBe("uncounted");
  });
});

describe("hostLensWarning", () => {
  it("says the ring is the scope even when the operator picked machines", () => {
    // `patch_approvals` has no device column. Picking three Linux boxes and
    // clearing their updates clears them for every machine in the ring, which
    // on `broad` is the fleet.
    expect(hostLensWarning(["srv-a", "srv-b"], "broad", 227)).toBe(
      "You are choosing these from srv-a and srv-b, but an approval covers all 227 devices in the broad ring — not those hosts alone.",
    );
  });

  it("stops naming machines once the list stops being readable", () => {
    expect(hostLensWarning(["a", "b", "c", "d", "e"], "test", 12)).toMatch(
      /^You are choosing these from 5 selected hosts,/,
    );
  });
});

describe("severitiesFromParam takes its default from the tab", () => {
  // One constant cannot be right on three platforms: apt's `important` IS the
  // security pocket, Microsoft leaves most cumulative updates unrated, and
  // Apple publishes no severity at all. See `defaultSeveritiesFor`.
  it("resolves an absent parameter against the platform's own default", () => {
    expect(severitiesFromParam(null, defaultSeveritiesFor("linux"))).toEqual([
      "critical",
      "important",
    ]);
    expect(severitiesFromParam(null, defaultSeveritiesFor("windows"))).toEqual([]);
    expect(severitiesFromParam(null, defaultSeveritiesFor("macos"))).toEqual([]);
  });

  it("still distinguishes an explicit 'off' from an absent parameter", () => {
    // The two have to stay distinguishable or the widening control on the Linux
    // tab could not be linked to.
    expect(severitiesFromParam("all", defaultSeveritiesFor("linux"))).toEqual([]);
    expect(severitiesFromParam("critical", defaultSeveritiesFor("windows"))).toEqual(["critical"]);
  });
});

describe("patchFamilyFromParam", () => {
  it("accepts only the three families `Patch.os_family` can hold", () => {
    expect(patchFamilyFromParam("linux")).toBe("linux");
    expect(patchFamilyFromParam("macos")).toBe("macos");
  });

  it("falls back to a STABLE default rather than a data-dependent one", () => {
    // "The tab with the most outstanding work" would move under the operator
    // between two visits and could not be linked to. The cost of a fixed
    // default is paid off by the count on every tab.
    expect(patchFamilyFromParam(null)).toBe("windows");
    expect(patchFamilyFromParam("solaris")).toBe("windows");
  });
});

describe("machinesNeedingUpdates", () => {
  it("agrees with itself about number, which the first draft did not", () => {
    // "1 device of 60 Windows machines with an agent need at least one update"
    // shipped in the browser pass. The subject is the count, so the verb has to
    // follow it.
    expect(machinesNeedingUpdates(1, 60, "Windows")).toBe(
      "1 of 60 Windows machines with an agent needs at least one update.",
    );
    expect(machinesNeedingUpdates(12, 60, "Windows")).toBe(
      "12 of 60 Windows machines with an agent need at least one update.",
    );
  });

  it("drops the denominator rather than inventing one it cannot read", () => {
    // `devices.read` absent. `devices_total` from the patch API is the whole
    // ORG whatever `os_family` was filtered on, so substituting it here would
    // measure one platform's machines against a fleet that is mostly not it.
    expect(machinesNeedingUpdates(3, null, "Linux")).toBe(
      "3 machines need at least one update.",
    );
  });
});
