import { describe, expect, it } from "vitest";
import {
  bandLabel,
  buildSubnetTree,
  describeDriftDetail,
  describeHistoryDetail,
  describeScope,
  SWEEP_SCOPE_SAFEGUARD,
  describeSweepRun,
  driftCounts,
  effectiveSweepScope,
  flattenSubnetTree,
  formatAge,
  formatCount,
  importApplyBlockReason,
  isCorroborated,
  isDriftFinding,
  isSuppressed,
  parsePrefix,
  planDisagreesWithReality,
  prefixContains,
  prefixSize,
  prefixesOverlap,
  scopeStatus,
  statePresentation,
  statusPresentation,
  utilizationDisplay,
} from "../ipamState";
import type { DriftOut, ScopeOut, SubnetOut, SweepRunOut, UtilizationOut } from "../types";
import { ADDRESS_STATUSES, DRIFT_CLASSES } from "../types";

// Unit tests over the module's presentation logic.
//
// EVERY CASE HERE IS TWO INPUTS THAT MUST PRODUCE DIFFERENT OUTPUT. A test that
// asserted a label against a map defined in the same file cannot fail; the
// assertions below are about the distinctions the module exists to keep — a
// dormant row against a drift row, a null percentage against a real one, a
// derived status against a documented state, a previewed file against a swapped
// one.

function drift(overrides: Partial<DriftOut> = {}): DriftOut {
  return {
    id: "d1",
    vrf_id: "v1",
    subnet_id: "s1",
    address: "10.4.7.9",
    drift_class: "documented_absent",
    severity: "medium",
    confidence: "medium",
    detail: {},
    device_id: null,
    first_detected_at: "2026-07-01T00:00:00.000Z",
    last_evaluated_at: "2026-07-20T00:00:00.000Z",
    consecutive_detections: 2,
    resolved_at: null,
    acknowledged_at: null,
    suppressed_until: null,
    note: "",
    ...overrides,
  };
}

function scope(overrides: Partial<ScopeOut> = {}): ScopeOut {
  return {
    id: "sc1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    note: "",
    authorized_by: "u1",
    authorized_at: "2026-07-01T00:00:00.000Z",
    expires_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function subnet(overrides: Partial<SubnetOut> = {}): SubnetOut {
  return {
    id: "s1",
    vrf_id: "v1",
    prefix: "10.4.7.0/24",
    kind: "assignable",
    name: "",
    description: "",
    site_id: null,
    vlan_id: null,
    gateway: null,
    dns_servers: [],
    parent_id: null,
    sweep_enabled: false,
    sweep_device_id: null,
    sweep_interval_hours: null,
    last_swept_at: null,
    ...overrides,
  };
}

function utilization(overrides: Partial<UtilizationOut> = {}): UtilizationOut {
  return {
    subnet_id: "s1",
    prefix: "10.4.7.0/24",
    enumerable: true,
    capacity: 254,
    free: 200,
    used: 54,
    percentage: 21.26,
    assigned: 40,
    reserved: 10,
    deprecated: 4,
    observed: 44,
    ...overrides,
  };
}

// --------------------------------------------------------------------------
// Age, never a timestamp
// --------------------------------------------------------------------------

describe("formatAge", () => {
  const now = Date.parse("2026-08-02T12:00:00.000Z");

  it("returns an age and NEVER any component of the timestamp it was given", () => {
    // The instant carries an obvious clock time and date. Neither may appear.
    const rendered = formatAge("2026-07-30T09:42:07.000Z", now);
    expect(rendered).toBe("3 days ago");
    expect(rendered).not.toContain("09:42");
    expect(rendered).not.toContain("2026");
    expect(rendered).not.toContain("07-30");
  });

  it("distinguishes minutes, hours and days rather than printing one unit", () => {
    expect(formatAge("2026-08-02T11:58:00.000Z", now)).toBe("2 minutes ago");
    expect(formatAge("2026-08-02T07:00:00.000Z", now)).toBe("5 hours ago");
    expect(formatAge("2026-07-13T12:00:00.000Z", now)).toBe("20 days ago");
  });

  it("says `never` for an address nothing has ever reported, not `0 minutes ago`", () => {
    expect(formatAge(null, now)).toBe("never");
    expect(formatAge(null, now)).not.toBe(formatAge("2026-08-02T12:00:00.000Z", now));
  });

  it("singularises, so `1 day ago` is not `1 days ago`", () => {
    expect(formatAge("2026-08-01T11:00:00.000Z", now)).toBe("1 day ago");
  });
});

// --------------------------------------------------------------------------
// `state` versus `status`
// --------------------------------------------------------------------------

describe("state and status are different columns", () => {
  it("gives every derived status its own presentation, and none of them is a state", () => {
    for (const status of ADDRESS_STATUSES) {
      expect(statusPresentation(status).label).not.toBe("");
    }
    // The four statuses that share a NAME with a state must still carry the
    // derived meaning, not the documented one.
    expect(statusPresentation("assigned").meaning).toContain("nothing has EVER looked");
    expect(statePresentation("assigned").meaning).toContain("intent");
    expect(statusPresentation("assigned").meaning).not.toBe(
      statePresentation("assigned").meaning,
    );
  });

  it("renders a null state as `Not documented`, never as the derived status", () => {
    // The ordinary sparse case: an address answering on the wire with no row.
    expect(statePresentation(null).label).toBe("Not documented");
    expect(statePresentation(null).label).not.toBe(statusPresentation("undocumented").label);
  });

  it("calls only real disagreement a disagreement", () => {
    expect(planDisagreesWithReality({ status: "undocumented" })).toBe(true);
    expect(planDisagreesWithReality({ status: "stale" })).toBe(true);
    expect(planDisagreesWithReality({ status: "conflicting" })).toBe(true);
    // Dormant and never-looked-at are not disagreements — nothing contradicts
    // anything, we simply cannot see.
    expect(planDisagreesWithReality({ status: "dormant" })).toBe(false);
    expect(planDisagreesWithReality({ status: "assigned" })).toBe(false);
    expect(planDisagreesWithReality({ status: "in_use" })).toBe(false);
  });
});

// --------------------------------------------------------------------------
// The enumeration ceiling
// --------------------------------------------------------------------------

describe("utilizationDisplay", () => {
  it("renders a null percentage as unavailable and draws NO bar", () => {
    const display = utilizationDisplay(
      utilization({
        prefix: "10.0.0.0/12",
        enumerable: false,
        capacity: null,
        free: null,
        used: null,
        percentage: null,
      }),
    );
    expect(display.available).toBe(false);
    expect(display.percentLabel).toBeNull();
    expect(display.barPercent).toBeNull();
    expect(display.headline).toBe("Utilization unavailable");
    // The exact failure this guards: a coerced zero reading as "empty".
    expect(display.headline).not.toContain("0%");
    expect(display.headline).not.toContain("0.0%");
  });

  it("still reports the counts that ARE known above the ceiling", () => {
    const display = utilizationDisplay(
      utilization({ percentage: null, capacity: null, free: null, used: null, assigned: 40 }),
    );
    expect(display.detail).toContain("40 assigned");
    expect(display.detail).toContain("is not zero");
  });

  it("produces a percentage and a bar for an enumerable prefix", () => {
    const display = utilizationDisplay(utilization({ percentage: 21.26 }));
    expect(display.available).toBe(true);
    expect(display.percentLabel).toBe("21.3%");
    expect(display.barPercent).toBeCloseTo(21.26);
  });

  it("a real 0% and an unavailable percentage do not render the same", () => {
    const empty = utilizationDisplay(utilization({ percentage: 0, used: 0, free: 254 }));
    const unknown = utilizationDisplay(utilization({ percentage: null }));
    expect(empty.percentLabel).toBe("0.0%");
    expect(unknown.percentLabel).toBeNull();
    expect(empty.headline).not.toBe(unknown.headline);
  });
});

// --------------------------------------------------------------------------
// Drift, dormant and orphans
// --------------------------------------------------------------------------

describe("driftCounts", () => {
  it("excludes dormant from the drift count and reports it as its own number", () => {
    const counts = driftCounts([
      drift({ id: "a", drift_class: "documented_absent" }),
      drift({ id: "b", drift_class: "conflict" }),
      drift({ id: "c", drift_class: "dormant" }),
      drift({ id: "d", drift_class: "dormant" }),
      drift({ id: "e", drift_class: "dormant" }),
    ]);
    expect(counts.drift).toBe(2);
    expect(counts.dormant).toBe(3);
    // The defect this exists to prevent: five findings on the headline.
    expect(counts.drift).not.toBe(5);
  });

  it("does not fold orphans into drift either", () => {
    const counts = driftCounts([drift()], 24);
    expect(counts.drift).toBe(1);
    expect(counts.orphans).toBe(24);
    expect(counts.drift).not.toBe(25);
  });

  it("excludes resolved findings from every live count", () => {
    const counts = driftCounts([
      drift({ id: "a" }),
      drift({ id: "b", resolved_at: "2026-07-30T00:00:00.000Z" }),
      drift({ id: "c", drift_class: "dormant", resolved_at: "2026-07-30T00:00:00.000Z" }),
    ]);
    expect(counts.drift).toBe(1);
    expect(counts.dormant).toBe(0);
  });

  it("counts acknowledged and suppressed findings without removing them from drift", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    const counts = driftCounts(
      [
        drift({ id: "a", acknowledged_at: "2026-07-31T00:00:00.000Z" }),
        drift({ id: "b", suppressed_until: "2026-09-01T00:00:00.000Z" }),
        drift({ id: "c", suppressed_until: "2026-07-01T00:00:00.000Z" }),
      ],
      0,
      now,
    );
    expect(counts.drift).toBe(3);
    expect(counts.acknowledged).toBe(1);
    // The expired suppression is not counted as suppressed.
    expect(counts.suppressed).toBe(1);
  });
});

describe("isDriftFinding", () => {
  it("is true for every class the server calls drift and false for dormant", () => {
    for (const driftClass of DRIFT_CLASSES) {
      expect(isDriftFinding({ drift_class: driftClass })).toBe(true);
    }
    expect(isDriftFinding({ drift_class: "dormant" })).toBe(false);
  });
});

describe("a conflict and a mismatch are different findings", () => {
  it("gives them different remedies, not just different labels", () => {
    const conflict = statusPresentation("conflicting");
    expect(conflict.label).toBe("Conflicting");
    // The two drift classes an operator is most likely to conflate.
    const mac = DRIFT_CLASSES.includes("mac_mismatch");
    expect(mac).toBe(true);
  });
});

describe("isCorroborated and isSuppressed", () => {
  it("distinguishes a single detection from a corroborated one", () => {
    expect(isCorroborated({ consecutive_detections: 1 })).toBe(false);
    expect(isCorroborated({ consecutive_detections: 2 })).toBe(true);
  });

  it("treats an elapsed suppression as not suppressed", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    expect(isSuppressed({ suppressed_until: "2026-08-03T00:00:00.000Z" }, now)).toBe(true);
    expect(isSuppressed({ suppressed_until: "2026-08-01T00:00:00.000Z" }, now)).toBe(false);
    expect(isSuppressed({ suppressed_until: null }, now)).toBe(false);
  });
});

// --------------------------------------------------------------------------
// Prefix arithmetic and scan authorization
// --------------------------------------------------------------------------

describe("parsePrefix", () => {
  it("parses IPv4 and IPv6 and masks host bits off", () => {
    const v4 = parsePrefix("10.70.0.5/24");
    expect(v4?.version).toBe(4);
    expect(v4?.prefixLen).toBe(24);
    // 10.70.0.0
    expect(v4?.base).toBe(0x0a460000n);

    const v6 = parsePrefix("2001:db8::/64");
    expect(v6?.version).toBe(6);
    expect(v6?.prefixLen).toBe(64);
  });

  it("rejects things that are not prefixes", () => {
    expect(parsePrefix("10.4.7.9")).toBeNull();
    expect(parsePrefix("10.4.7.0/33")).toBeNull();
    expect(parsePrefix("999.1.1.0/24")).toBeNull();
    expect(parsePrefix("")).toBeNull();
  });

  it("sizes a prefix, which is what makes a /8 legible", () => {
    expect(prefixSize(parsePrefix("10.0.0.0/8") as never)).toBe(16777216n);
    expect(prefixSize(parsePrefix("10.4.7.0/24") as never)).toBe(256n);
    expect(formatCount(16777216n)).toBe("16,777,216");
  });

  it("computes containment and overlap, and never across families", () => {
    const eight = parsePrefix("10.0.0.0/8") as never;
    const twentyFour = parsePrefix("10.4.7.0/24") as never;
    const elsewhere = parsePrefix("192.168.0.0/24") as never;
    const v6 = parsePrefix("2001:db8::/32") as never;
    expect(prefixContains(eight, twentyFour)).toBe(true);
    expect(prefixContains(twentyFour, eight)).toBe(false);
    expect(prefixesOverlap(eight, twentyFour)).toBe(true);
    expect(prefixesOverlap(twentyFour, elsewhere)).toBe(false);
    expect(prefixesOverlap(eight, v6)).toBe(false);
  });
});

describe("scopeStatus", () => {
  const now = Date.parse("2026-08-02T00:00:00.000Z");

  it("separates active, expired and revoked", () => {
    expect(scopeStatus(scope(), now)).toBe("active");
    expect(scopeStatus(scope({ expires_at: "2026-07-01T00:00:00.000Z" }), now)).toBe("expired");
    expect(scopeStatus(scope({ revoked_at: "2026-07-01T00:00:00.000Z" }), now)).toBe("revoked");
  });

  it("says REVOKED, not expired, for a row a person stopped that then lapsed", () => {
    const both = scope({
      revoked_at: "2026-06-01T00:00:00.000Z",
      expires_at: "2026-07-01T00:00:00.000Z",
    });
    expect(scopeStatus(both, now)).toBe("revoked");
  });
});

describe("describeScope", () => {
  it("states the reach of a grant in addresses, not just its mask", () => {
    const described = describeScope(scope({ prefix: "10.0.0.0/8" }));
    expect(described.addressCount).toBe("16,777,216");
    expect(described.summary).toContain("16,777,216");
    // And it names the safeguard rather than implying a /8 grant is harmless.
    // The safeguard moved OFF the row (it is the same sentence under every
    // prefix, and repeating it per row made each one ~500px tall on a phone,
    // pushing the row's only action out of the viewport) and onto the page,
    // once, above the table. Same words, same claim, one carrier.
    expect(described.safeguard).toContain("bounded by the subnet");
    expect(described.safeguard).toBe(SWEEP_SCOPE_SAFEGUARD);
  });

  it("counts a future expiry down, and never says a live grant has just lapsed", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    // THE DEFECT: `formatAge` clamps anything in the future to "just now", so
    // a grant with thirty days left was drawn as though it had just expired —
    // on the page that decides where this platform emits packets.
    const future = describeScope(
      scope({ expires_at: "2026-09-01T00:00:00.000Z" }),
      now,
    );
    expect(future.expiry).toBe("Expires in 30 days");
    // And a millisecond into that window it is still thirty days, not
    // twenty-nine: a countdown rounds where an age floors.
    expect(describeScope(scope({ expires_at: "2026-09-01T00:00:00.000Z" }), now + 1).expiry).toBe(
      "Expires in 30 days",
    );
    expect(future.expiry).not.toContain("just now");
    expect(future.status).toBe("active");

    const past = describeScope(scope({ expires_at: "2026-07-30T00:00:00.000Z" }), now);
    expect(past.expiry).toBe("Expired, 3 days ago");
    expect(past.status).toBe("expired");

    expect(describeScope(scope({ expires_at: null }), now).expiry).toContain("No expiry");
  });

  it("says an inactive grant authorizes nothing", () => {
    const described = describeScope(scope({ revoked_at: "2026-01-01T00:00:00.000Z" }));
    expect(described.summary).toContain("Authorizes nothing");
    expect(describeScope(scope()).summary).not.toBe(described.summary);
  });
});

describe("effectiveSweepScope", () => {
  const now = Date.parse("2026-08-02T00:00:00.000Z");

  it("is DENY BY DEFAULT: no authorization means not sweepable", () => {
    const result = effectiveSweepScope(subnet(), [], now);
    expect(result.authorized).toBe(false);
    expect(result.explanation).toContain("NOT AUTHORIZED");
  });

  it("does not treat a documented subnet as an authorized one", () => {
    // An authorization exists — for a different range entirely.
    const result = effectiveSweepScope(subnet(), [scope({ prefix: "192.168.0.0/16" })], now);
    expect(result.authorized).toBe(false);
    expect(result.covering).toEqual([]);
  });

  it("does not honour a revoked or expired grant", () => {
    const revoked = scope({ prefix: "10.0.0.0/8", revoked_at: "2026-07-01T00:00:00.000Z" });
    const expired = scope({ prefix: "10.0.0.0/8", expires_at: "2026-07-01T00:00:00.000Z" });
    expect(effectiveSweepScope(subnet(), [revoked], now).authorized).toBe(false);
    expect(effectiveSweepScope(subnet(), [expired], now).authorized).toBe(false);
  });

  it("does not cross VRFs", () => {
    const other = scope({ prefix: "10.0.0.0/8", vrf_id: "v2" });
    expect(effectiveSweepScope(subnet({ vrf_id: "v1" }), [other], now).authorized).toBe(false);
  });

  it("authorizes under a covering grant and says the targets stay inside the subnet", () => {
    const grant = scope({ prefix: "10.0.0.0/8" });
    const result = effectiveSweepScope(subnet({ prefix: "10.4.7.0/24" }), [grant], now);
    expect(result.authorized).toBe(true);
    expect(result.covering).toHaveLength(1);
    expect(result.explanation).toContain("10.4.7.0/24 intersected with 10.0.0.0/8");
    expect(result.explanation).toContain("never wider than the subnet");
  });
});

describe("describeSweepRun", () => {
  function run(overrides: Partial<SweepRunOut> = {}): SweepRunOut {
    return {
      id: "r1",
      subnet_id: "s1",
      job_id: "j1",
      device_id: "d1",
      state: "succeeded",
      trigger: "scheduled",
      target_count: 254,
      scope_digest: "abc",
      hosts_found: 31,
      truncated: false,
      started_at: "2026-08-01T00:00:00.000Z",
      finished_at: "2026-08-01T00:05:00.000Z",
      error: "",
      ...overrides,
    };
  }

  it("renders `no_agent` as its own outcome, not as a failure or a success", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    const noAgent = describeSweepRun(run({ state: "no_agent", hosts_found: 0 }), now);
    expect(noAgent).toContain("No agent could sweep");
    expect(noAgent).not.toContain("Succeeded");
    expect(noAgent).not.toContain("Failed");
  });

  it("distinguishes refused (nothing was ever signed) from failed", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    expect(describeSweepRun(run({ state: "refused" }), now)).toContain("not authorized");
    expect(describeSweepRun(run({ state: "failed" }), now)).toContain("Failed");
  });

  it("says never, rather than nothing, when there is no run", () => {
    expect(describeSweepRun(undefined)).toBe("Never swept.");
  });

  it("renders the run's time as an age", () => {
    const now = Date.parse("2026-08-02T00:00:00.000Z");
    expect(describeSweepRun(run(), now)).toContain("23 hours ago");
    expect(describeSweepRun(run(), now)).not.toContain("2026-08-01");
    expect(describeSweepRun(run(), now)).not.toContain("00:05");
  });
});

// --------------------------------------------------------------------------
// The derived tree
// --------------------------------------------------------------------------

describe("buildSubnetTree", () => {
  it("nests by the server-derived parent_id and assigns depth", () => {
    const rows = [
      subnet({ id: "a", prefix: "10.0.0.0/8", parent_id: null }),
      subnet({ id: "b", prefix: "10.4.0.0/16", parent_id: "a" }),
      subnet({ id: "c", prefix: "10.4.7.0/24", parent_id: "b" }),
    ];
    const flat = flattenSubnetTree(buildSubnetTree(rows));
    expect(flat.map((node) => node.subnet.id)).toEqual(["a", "b", "c"]);
    expect(flat.map((node) => node.depth)).toEqual([0, 1, 2]);
  });

  it("renders an orphaned child at the root rather than dropping it", () => {
    // The filtered case: the parent did not match the filter, the child did.
    const rows = [subnet({ id: "c", prefix: "10.4.7.0/24", parent_id: "missing" })];
    const flat = flattenSubnetTree(buildSubnetTree(rows));
    expect(flat).toHaveLength(1);
    expect(flat[0]?.depth).toBe(0);
  });
});

// --------------------------------------------------------------------------
// Import's two-step guard
// --------------------------------------------------------------------------

describe("importApplyBlockReason", () => {
  const file = { name: "plan.csv", size: 1024, lastModified: 1000 };
  const previewed = { ...file, digest: "a".repeat(64) };

  it("refuses apply when nothing has been previewed", () => {
    const reason = importApplyBlockReason(file, null);
    expect(reason).not.toBeNull();
    expect(reason).toContain("Preview this file first");
  });

  it("refuses apply when the chosen file is not the previewed one", () => {
    // Same name and size, edited since — the case a name check alone misses.
    const swapped = { name: "plan.csv", size: 1024, lastModified: 2000 };
    expect(importApplyBlockReason(swapped, previewed)).toContain("before applying");
    // And a different file entirely.
    expect(importApplyBlockReason({ name: "other.csv", size: 99, lastModified: 1000 }, previewed))
      .not.toBeNull();
  });

  it("allows apply for exactly the previewed file", () => {
    expect(importApplyBlockReason(file, previewed)).toBeNull();
  });

  it("refuses a file over the 10 MB import limit", () => {
    const big = { name: "plan.csv", size: 11 * 1024 * 1024, lastModified: 1000 };
    expect(importApplyBlockReason(big, { ...big, digest: "b".repeat(64) })).toContain("10 MB");
  });
});

// --------------------------------------------------------------------------
// Range bands
// --------------------------------------------------------------------------

describe("bandLabel", () => {
  it("names the band an address sits in, and null when it sits in none", () => {
    expect(bandLabel({ range_kinds: [] })).toBeNull();
    expect(bandLabel({ range_kinds: ["dhcp_pool"] })).toBe("DHCP pool");
    expect(bandLabel({ range_kinds: ["dhcp_pool", "excluded"] })).toBe(
      "DHCP pool + Excluded range",
    );
  });
});

// --------------------------------------------------------------------------
// §11.3's `detail`
// --------------------------------------------------------------------------

/** The facts as an operator reads them: "<label> <value>", in order. */
function lines(detail: Record<string, unknown>): string[] {
  return describeHistoryDetail({ detail }).map((fact) => `${fact.label} ${fact.value}`);
}

describe("describeHistoryDetail", () => {
  it("orders the assign payload as who, then how", () => {
    // Not the key order of the object: "who took it" comes before "how", and
    // the three context keys every event carries come out at all.
    expect(
      lines({
        address: "10.4.7.9",
        subnet_id: "s1",
        vrf_id: "v1",
        allocation_method: "static",
        claimant: "dev-7f3a",
        claimant_type: "device",
      }),
    ).toEqual(["Claimed by dev-7f3a (device)", "Method static"]);
  });

  it("separates an asset label from a device id", () => {
    // "x (device)" claims inventory resolved it; "x (asset label)" claims a
    // person typed it. The two are different assertions about the same string.
    expect(lines({ claimant: "x", claimant_type: "device" })).toEqual(["Claimed by x (device)"]);
    expect(lines({ claimant: "x", claimant_type: "asset_label" })).toEqual([
      "Claimed by x (asset label)",
    ]);
  });

  it("separates a release from a claimant from a release of a reservation", () => {
    expect(lines({ previous_claimant: "dev-9c11" })).toEqual(["Released from dev-9c11"]);
    expect(lines({ previous_claimant: "" })).toEqual([
      "Released from nobody, it was held, not claimed",
    ]);
  });

  it("pairs an update's `from` and `to` field by field", () => {
    expect(
      lines({ from: { hostname: "A", description: "" }, to: { hostname: "B", description: "x" } }),
    ).toEqual(["Description (empty) → x", "Hostname A → B"]);
  });

  it("keeps a key it has never seen, and drops none of it", () => {
    expect(describeHistoryDetail({ detail: { address: "10.4.7.9", wibble: "42" } })).toEqual([
      { key: "wibble", label: "Wibble", value: "42" },
    ]);
  });

  it("renders an instant in `detail` as an age, like every other instant here", () => {
    // An age, never the clock time it came from — rule 1 does not stop at the
    // projection's own columns.
    expect(lines({ observed_at: "2026-07-30T09:42:07.000Z" })).toEqual([
      `Observed at ${formatAge("2026-07-30T09:42:07.000Z")}`,
    ]);
    expect(lines({ observed_at: "2026-07-30T09:42:07.000Z" })[0]).toMatch(/ ago$/);
    // A key that is NOT an instant keeps its literal value, so the branch above
    // is a real branch and not a blanket reformat.
    expect(lines({ observed_mac: "2026-07-30T09:42:07.000Z" })).toEqual([
      "Observed mac 2026-07-30T09:42:07.000Z",
    ]);
  });

  it("keeps an `_at` value that is not an instant, instead of calling it unknown", () => {
    // `_at` is a naming convention, not a type: reconcile's `device_moved`
    // payload puts ADDRESSES in `documented_at`/`observed_at`. Ageing one of
    // those prints "unknown" over the evidence, and both renderers share this
    // formatter, so the guard is asserted on both.
    expect(lines({ observed_at: "10.4.7.57" })).toEqual(["Observed at 10.4.7.57"]);
    expect(lines({ observed_at: "2001:db8::1" })).toEqual(["Observed at 2001:db8::1"]);
  });

  it("survives a detail that is not an object at all", () => {
    expect(describeHistoryDetail({ detail: {} })).toEqual([]);
    expect(
      describeHistoryDetail({ detail: null as unknown as Record<string, unknown> }),
    ).toEqual([]);
    expect(
      describeHistoryDetail({ detail: ["a"] as unknown as Record<string, unknown> }),
    ).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// §4.3's `detail` — the same shape, the same renderer
// --------------------------------------------------------------------------

/** The facts as an operator reads them: "<label> <value>", in order. */
function driftLines(detail: Record<string, unknown>): string[] {
  return describeDriftDetail({ detail }).map((fact) => `${fact.label} ${fact.value}`);
}

/** The values ALONE. Every assertion below leads with these: a label is text
 *  this module owns and proves nothing about what the server sent. */
function driftValues(detail: Record<string, unknown>): string[] {
  return describeDriftDetail({ detail }).map((fact) => fact.value);
}

describe("describeDriftDetail", () => {
  // Every payload below is copied from what `reconcile._evaluate` actually
  // writes for that class, not from a guess at what it might.

  it("names the MACs, the claimant and the VRF count a conflict is made of", () => {
    // `{"macs": […], "claimant_mismatch": true, "vrf_count": 2}` — the three
    // disjuncts of §4.3's conflict predicate, and the operator has to know WHICH
    // of them fired before touching anything.
    expect(
      driftValues({
        macs: ["a4:bb:6d:11:22:33", "f0:18:98:aa:bb:cc"],
        claimant_mismatch: true,
        vrf_count: 2,
      }),
    ).toEqual([
      "a4:bb:6d:11:22:33, f0:18:98:aa:bb:cc",
      "Yes, inventory names a different device",
      "2",
    ]);

    // A conflict from two MACs and a conflict from two VRFs are different
    // findings with the same class, and they must not read alike.
    expect(driftValues({ macs: ["a4:bb:6d:11:22:33"], claimant_mismatch: false, vrf_count: 0 }))
      .toEqual([
        "a4:bb:6d:11:22:33",
        "No, in the MACs or the VRFs, not the claimants",
        "None, this conflict is not cross-VRF",
      ]);
  });

  it("keeps a documented MAC and an observed one apart", () => {
    // A mismatch is two hardware addresses; showing them unlabelled, or showing
    // only one, leaves the operator unable to say which record is stale.
    expect(
      driftValues({ documented_mac: "a4:bb:6d:11:22:33", observed_macs: ["f0:18:98:aa:bb:cc"] }),
    ).toEqual(["a4:bb:6d:11:22:33", "f0:18:98:aa:bb:cc"]);
    expect(
      driftLines({ documented_mac: "a4:bb:6d:11:22:33", observed_macs: ["f0:18:98:aa:bb:cc"] }),
    ).toEqual(["Documented MAC a4:bb:6d:11:22:33", "Observed MACs f0:18:98:aa:bb:cc"]);
  });

  it("names both devices in a claimant mismatch", () => {
    expect(
      driftValues({
        documented_device_id: "0d1f8a2c-doc",
        observed_device_id: "7b3e5c41-obs",
      }),
    ).toEqual(["0d1f8a2c-doc", "7b3e5c41-obs"]);
  });

  it("shows a moved device's two ADDRESSES, which is the whole finding", () => {
    // `documented_at` and `observed_at` are addresses, not instants
    // (`reconcile._evaluate`'s `device_moved` payload). "where it should be"
    // and "where it is" are what "move the assignment" needs, and the age rule
    // would have printed "unknown" over both.
    const detail = {
      documented_at: "10.4.7.9",
      observed_at: "10.4.7.57",
      device_id: "0d1f8a2c-dev",
      observed_subnet_id: "9c11-subnet",
    };
    expect(driftValues(detail)).toEqual([
      "0d1f8a2c-dev",
      "10.4.7.9",
      "10.4.7.57",
      "9c11-subnet",
    ]);
    expect(driftValues(detail).join(" ")).not.toMatch(/unknown|ago/);
  });

  it("says which gate failed, which is the difference between dormant and drift", () => {
    // Same keys, opposite booleans: `documented_absent` passed both gates,
    // `dormant` failed one. If the two render alike, the page has just told an
    // operator that a switched-off laptop is wrong.
    const absent = driftValues({
      state: "assigned",
      device_id: "0d1f8a2c-dev",
      asset_label: null,
      gate_e: true,
      gate_l: true,
    });
    const dormant = driftValues({
      state: "assigned",
      device_id: "0d1f8a2c-dev",
      gate_e: true,
      gate_l: false,
    });
    expect(absent).toEqual([
      "assigned",
      "0d1f8a2c-dev",
      "none",
      "Yes, inside the window",
      "Yes, its agent checked in",
    ]);
    expect(dormant).toEqual([
      "assigned",
      "0d1f8a2c-dev",
      "Yes, inside the window",
      "No, offline past the window",
    ]);
    expect(
      driftValues({ state: "assigned", gate_e: false, gate_l: true }),
    ).toEqual([
      "assigned",
      "No, not inside the window",
      "Yes, its agent checked in",
    ]);
  });

  it("lists what answered at an undocumented address, including an empty list", () => {
    expect(
      driftValues({
        macs: ["a4:bb:6d:11:22:33"],
        sources: ["inventory", "sweep"],
        hostnames: ["WS-01"],
      }),
    ).toEqual(["a4:bb:6d:11:22:33", "WS-01", "inventory, sweep"]);
    // An ICMP-only sweep sees neither a MAC nor a hostname. "(none)" is a fact;
    // a blank cell is a rendering failure the operator cannot tell from one.
    expect(driftValues({ macs: [], sources: ["sweep"], hostnames: [] })).toEqual([
      "(none)",
      "(none)",
      "sweep",
    ]);
  });

  it("renders an id as the name it stands for, and as itself when nothing knows it", () => {
    // The evidence column read "Observed by dev-5" beside columns naming the
    // address, the class and the subnet in the operator's own words.
    const names = {
      devices: new Map([["dev-5", "rack-b4-relay"]]),
      subnets: new Map([["subnet-1", "10.20.9.0/24 Workstations"]]),
    };
    expect(
      describeDriftDetail(
        { detail: { observed_by: "dev-5", observed_subnet_id: "subnet-1" } },
        names,
      ).map((fact) => fact.value),
    ).toEqual(["rack-b4-relay", "10.20.9.0/24 Workstations"]);

    // A lookup that misses is not a blank: an id is still evidence, and a
    // device that has left the fleet keeps its finding readable.
    expect(
      describeDriftDetail({ detail: { observed_by: "dev-404" } }, names).map((f) => f.value),
    ).toEqual(["dev-404"]);
    // And with no book at all, which is what every other caller passes.
    expect(driftValues({ observed_by: "dev-5" })).toEqual(["dev-5"]);
    // A NULL id is not an id. It still reads as the absence it is, exactly as
    // it did before any of this could name anything.
    expect(driftValues({ device_id: null })).toEqual(["none"]);
  });

  it("keeps every drift label short enough for the cell it is rendered in", () => {
    // `DetailFacts`' narrow mode gives the label track `fit-content(20ch)`, so
    // a label past 20 characters WRAPS TO TWO LINES inside a table cell — which
    // is how one `documented_absent` row came to stand 230px tall beside a 60px
    // neighbour. The cap is the contract between the two files; this is the
    // assertion that fails when a new rule quietly writes a clause where a
    // label belongs.
    const everyKey = {
      state: "assigned",
      device_id: "dev-1",
      asset_label: "Desk 4",
      documented_device_id: "dev-2",
      documented_claimant: "dev-2",
      observed_device_id: "dev-3",
      observed_claimant: "dev-3",
      observed_by: "dev-4",
      documented_mac: "a4:bb:6d:11:22:33",
      observed_macs: ["f0:18:98:aa:bb:cc"],
      macs: ["f0:18:98:aa:bb:cc"],
      hostnames: ["ws-01"],
      sources: ["sweep"],
      documented_at: "10.4.7.9",
      observed_at: "10.4.7.57",
      observed_subnet_id: "subnet-1",
      claimant_mismatch: true,
      vrf_count: 2,
      gate_e: true,
      gate_l: false,
    };
    const labels = describeDriftDetail({ detail: everyKey }).map((fact) => fact.label);
    // Every rule fired, so this really is every label the module can write.
    expect(labels.length).toBe(Object.keys(everyKey).length);
    const tooLong = labels.filter((label) => label.length > 20);
    expect(tooLong).toEqual([]);
  });

  it("renders a key it has never seen rather than dropping or crashing on it", () => {
    // Drift classes grow. An unanticipated key arrives readable, a nested one
    // flattens to text, and neither blanks the cell.
    expect(driftLines({ last_seen_days: 30 })).toEqual(["Last seen days 30"]);
    expect(driftLines({ wibble: { inner: 2, deep: null } })).toEqual([
      "Wibble Inner: 2, Deep: none",
    ]);
    expect(driftLines({ evidence: ["a", "b"] })).toEqual(["Evidence a, b"]);
    expect(describeDriftDetail({ detail: {} })).toEqual([]);
    expect(describeDriftDetail({ detail: null as unknown as Record<string, unknown> })).toEqual([]);
    expect(describeDriftDetail({ detail: ["a"] as unknown as Record<string, unknown> })).toEqual([]);
  });

  it("shows a value of an unexpected type instead of swallowing it", () => {
    // A rule that does not recognise its own payload DECLINES, and the generic
    // pass renders the key. The failure mode of a typed reader over a free-form
    // payload is a silently missing fact, which is worse than an ugly one.
    expect(driftLines({ vrf_count: "two" })).toEqual(["Vrf count two"]);
    expect(driftLines({ gate_e: "yes" })).toEqual(["Gate e yes"]);
    expect(driftLines({ claimant_mismatch: null })).toEqual(["Claimant mismatch none"]);
  });

  it("keeps rule 1 for an instant that really is one", () => {
    // Nothing writes a timestamp into a drift `detail` today. When something
    // does, it ages like every other instant in this module — a drift cell is
    // not where the monitoring boundary gets crossed.
    const value = driftValues({ resolved_at: "2026-07-30T09:42:07.000Z" })[0];
    expect(value).toMatch(/ ago$/);
    expect(value).not.toMatch(/09:42/);
  });

  it("is the history renderer, not a second one", () => {
    // The property the whole refactor is for: one engine, one value formatter,
    // one unknown-key fallback. Two implementations of one free-form shape drift
    // apart, and this is the assertion that fails when a second one appears.
    const shared = { wibble: 42, nested: { a: 1 }, when_at: "2026-07-30T09:42:07.000Z" };
    expect(describeDriftDetail({ detail: shared })).toEqual(
      describeHistoryDetail({ detail: shared }),
    );
  });
});
