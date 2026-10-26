/**
 * The presentation logic behind the two push-install control screens.
 *
 * **WHY THESE ASSERTIONS AND NOT OTHERS.** Every one of them is about a claim
 * the screen MAKES to an operator who is about to authorize credentialed
 * logins on a network segment. A screen that renders the wrong number, or that
 * calls a revoked grant "expired", is not a cosmetic defect here: it is the
 * console misstating what the platform is permitted to do.
 *
 * `parsePrefix` is tested against the shapes a person actually types, and
 * against the ones that must NOT be silently accepted -- `10.0.0.0/24/8`
 * truncated to a /24 would be a grant that does not match what was typed.
 */

import { describe, expect, it } from "vitest";

import {
  siteServerCoverage,
  describeReach,
  formatCount,
  GRANT_STATUS_PRESENTATION,
  grantStatus,
  parsePrefix,
  prefixSize,
  scopeIsBound,
  windowsProbeReadiness,
  isWinrmRow,
  refusalReasonLabel,
  REFUSAL_REASON_LABELS,
  winrmTargetState,
  winrmInstallable,
} from "../pushState";
import type { HostKeyOut } from "../types";

const NOW = Date.parse("2026-08-26T12:00:00Z");

function grant(overrides: { revoked_at?: string | null; expires_at?: string | null }) {
  return { revoked_at: null, expires_at: null, ...overrides };
}

describe("grantStatus", () => {
  it("distinguishes revoked from expired from active", () => {
    // THREE FIXTURES, THREE DIFFERENT OBSERVABLE VALUES. A test with one row
    // could not tell "the status works" from "everything is active".
    expect(grantStatus(grant({}), NOW)).toBe("active");
    expect(grantStatus(grant({ expires_at: "2026-08-25T00:00:00Z" }), NOW)).toBe("expired");
    expect(grantStatus(grant({ revoked_at: "2026-08-20T00:00:00Z" }), NOW)).toBe("revoked");
  });

  it("calls a revoked grant revoked even when it also expired", () => {
    // Revocation is what a PERSON did; expiry is what a clock did. When both
    // are true the person is the answer an auditor wants.
    const both = grant({
      revoked_at: "2026-08-20T00:00:00Z",
      expires_at: "2026-08-21T00:00:00Z",
    });
    expect(grantStatus(both, NOW)).toBe("revoked");
  });

  it("treats an expiry exactly now as lapsed, not as active", () => {
    // The dangerous direction is the other one: a grant that authorizes
    // credentialed logins for one more render than it should.
    expect(grantStatus(grant({ expires_at: "2026-08-26T12:00:00Z" }), NOW)).toBe("expired");
  });

  it("gives every status a label and an explanation", () => {
    for (const status of ["active", "expired", "revoked"] as const) {
      const presentation = GRANT_STATUS_PRESENTATION[status];
      expect(presentation.label).not.toBe("");
      expect(presentation.meaning.length).toBeGreaterThan(40);
    }
  });
});

describe("scopeIsBound", () => {
  it("reads the stamp, because the row survives unbinding", () => {
    expect(scopeIsBound({ unbound_at: null })).toBe(true);
    expect(scopeIsBound({ unbound_at: "2026-08-20T00:00:00Z" })).toBe(false);
  });
});

describe("parsePrefix", () => {
  it("accepts a CIDR and a bare address", () => {
    expect(parsePrefix("10.4.7.0/24")).toEqual({ bits: 24, version: 4 });
    // A bare address is the narrowest useful grant and a common one here:
    // binding a credential to exactly one server is a legitimate scope.
    expect(parsePrefix("10.4.7.9")).toEqual({ bits: 32, version: 4 });
    expect(parsePrefix("  10.4.7.0/24  ")).toEqual({ bits: 24, version: 4 });
    expect(parsePrefix("2001:db8::/64")).toEqual({ bits: 64, version: 6 });
  });

  it("refuses what must not be silently accepted", () => {
    // `10.0.0.0/24/8` truncated to a /24 would be a grant that does not match
    // what the operator typed, and they would never see it happen.
    expect(parsePrefix("10.0.0.0/24/8")).toBeNull();
    expect(parsePrefix("10.0.0.256/24")).toBeNull();
    expect(parsePrefix("10.0.0.0/33")).toBeNull();
    expect(parsePrefix("10.0.0")).toBeNull();
    expect(parsePrefix("server-17.corp.example")).toBeNull();
    expect(parsePrefix("")).toBeNull();
    expect(parsePrefix("   ")).toBeNull();
  });
});

describe("prefixSize and formatCount", () => {
  it("sizes a prefix in addresses, grouped", () => {
    // THE WHOLE POINT OF THE COLUMN. "/8" is two characters and this many
    // machines, and nobody converts mask lengths in their head.
    expect(formatCount(prefixSize({ bits: 8, version: 4 }))).toBe("16,777,216");
    expect(formatCount(prefixSize({ bits: 24, version: 4 }))).toBe("256");
    expect(formatCount(prefixSize({ bits: 32, version: 4 }))).toBe("1");
  });

  it("survives an IPv6 prefix, which is not a Number", () => {
    // `2 ** 64` as a float is 18446744073709552000 -- wrong, and rendered
    // confidently. `bigint` is why this is exact.
    expect(formatCount(prefixSize({ bits: 64, version: 6 }))).toBe(
      "18,446,744,073,709,551,616",
    );
  });
});

describe("describeReach", () => {
  it("says one address in the singular, and a count otherwise", () => {
    expect(describeReach({ bits: 32, version: 4 })).toContain("exactly one address");
    expect(describeReach({ bits: 16, version: 4 })).toContain("65,536 addresses");
  });

  it("says what the grant puts in reach, in the operator's terms", () => {
    // Not "authorizes a prefix": the sentence has to name the consequence,
    // because the consequence is a credentialed login on somebody's machine.
    expect(describeReach({ bits: 24, version: 4 })).toContain("credentialed push install");
  });
});

describe("siteServerCoverage", () => {
  // The preflight refuses a target no enabled site server's segment contains
  // (`agent_push/preflight.py`, `no_site_server`), address by address. So a
  // range is served exactly when every address in it is inside SOME enabled
  // server's segment -- which may take more than one server.
  const server = (segments: string[], enabled = true) => ({ segments, enabled });

  it("serves a range inside one server's segment", () => {
    expect(siteServerCoverage("10.20.4.0/24", [server(["10.20.0.0/16"])])).toBe("served");
  });

  it("serves a range only the union of two servers covers", () => {
    expect(
      siteServerCoverage("10.4.6.0/23", [server(["10.4.6.0/24"]), server(["10.4.7.0/24"])]),
    ).toBe("served");
  });

  it("calls a range partly served when some of its addresses have no server", () => {
    expect(siteServerCoverage("10.4.0.0/16", [server(["10.4.7.0/24"])])).toBe("partial");
  });

  it("does not count a disabled server", () => {
    expect(siteServerCoverage("10.30.1.0/24", [server(["10.30.1.0/24"], false)])).toBe("none");
  });

  it("does not mistake a textual prefix for containment", () => {
    // 10.200.7.0/24 starts with "10.20" and is nowhere near 10.20.0.0/16.
    expect(siteServerCoverage("10.200.7.0/24", [server(["10.20.0.0/16"])])).toBe("none");
  });

  it("reads a bare address as one host", () => {
    expect(siteServerCoverage("10.20.4.17", [server(["10.20.4.0/24"])])).toBe("served");
  });

  it("never serves an IPv6 range: site server segments are IPv4 only", () => {
    expect(siteServerCoverage("2001:db8::/64", [server(["10.0.0.0/8"])])).toBe("ipv6");
  });
});

describe("windowsProbeReadiness", () => {
  // Design §2.4: a credential-free WinRM probe records whether WinRM answered
  // (5986 or 5985) and whether a domain controller was reachable from the site
  // server. The readiness panel reads it from the host-key list, where only a
  // Windows probe's row carries it -- an SSH row's defaults (false) say
  // nothing about Windows and must not be counted as a Windows machine that
  // failed.
  function row(address: string, overrides: Partial<HostKeyOut> = {}): HostKeyOut {
    return {
      address,
      transport: "winrm",
      observed_fingerprint: "winrm",
      observed_key_type: "",
      observed_at: "2026-09-28T00:00:00Z",
      confirmed_fingerprint: null,
      confirmed_at: null,
      confirmed_by: null,
      installable: false,
      winrm_https: false,
      winrm_http: false,
      kdc_reachable: false,
      ...overrides,
    };
  }

  it("counts only Windows probes, never an SSH row", () => {
    const readiness = windowsProbeReadiness([
      row("10.0.0.1", { transport: "ssh", observed_fingerprint: "SHA256:AAAA" }),
      row("10.0.0.2", { winrm_https: true, kdc_reachable: true }),
    ]);
    expect(readiness).toEqual({ probed: 1, winrm: 1, https: 1, http: 0, kdc: 1 });
  });

  it("counts either port as WinRM answering, once per machine", () => {
    const readiness = windowsProbeReadiness([
      row("10.0.0.2", { winrm_https: true, winrm_http: true }),
      row("10.0.0.3", { winrm_http: true }),
      row("10.0.0.4"),
    ]);
    expect(readiness).toEqual({ probed: 3, winrm: 2, https: 1, http: 2, kdc: 0 });
  });

  it("reads a row from a server that predates the readiness fields as not answered", () => {
    // A console newer than its API: absent is not true.
    const { winrm_https: _https, winrm_http: _http, kdc_reachable: _kdc, ...legacy } = row("10.0.0.5");
    expect(windowsProbeReadiness([legacy as HostKeyOut])).toEqual({ probed: 1, winrm: 0, https: 0, http: 0, kdc: 0 });
  });

  it("reads nothing probed as nothing probed", () => {
    expect(windowsProbeReadiness([])).toEqual({ probed: 0, winrm: 0, https: 0, http: 0, kdc: 0 });
  });

  it("knows a WinRM row by transport or by either marker, never an SSH key", () => {
    expect(isWinrmRow(row("a"))).toBe(true);
    expect(isWinrmRow(row("a", { transport: "ssh", observed_fingerprint: "kerberos" }))).toBe(true);
    expect(isWinrmRow(row("a", { transport: "ssh", observed_fingerprint: "SHA256:AAAA" }))).toBe(false);
  });

  // Review I2: KDC reachability is ADVISORY. The probe finds a domain
  // controller by DNS SRV for the target's domain; a credential that names its
  // domain controller explicitly never uses that lookup. Only WinRM answering
  // gates the install.
  it("is installable when WinRM answered on either port; a KDC not found via DNS only warns", () => {
    expect(winrmTargetState(undefined)).toBe("not_probed");
    expect(winrmTargetState(row("a", { kdc_reachable: true }))).toBe("no_winrm");
    expect(winrmTargetState(row("a", { winrm_http: true }))).toBe("kdc_not_found");
    expect(winrmTargetState(row("a", { winrm_http: true, kdc_reachable: true }))).toBe("ready");
    expect(winrmTargetState(row("a", { winrm_https: true, kdc_reachable: true }))).toBe("ready");
    expect(winrmInstallable("ready")).toBe(true);
    expect(winrmInstallable("kdc_not_found")).toBe(true);
    expect(winrmInstallable("no_winrm")).toBe(false);
    expect(winrmInstallable("not_probed")).toBe(false);
  });
});

describe("refusalReasonLabel", () => {
  it("names a known reason in plain words, never the code", () => {
    expect(refusalReasonLabel("no_site_server")).toMatch(/No site server serves/);
    expect(refusalReasonLabel("no_winrm_site_server")).toMatch(/too old to push to Windows/);
  });

  it("falls back to the server's remedy, then to a sentence, never to the code", () => {
    expect(refusalReasonLabel("brand_new", "something the server says")).toBe("Something the server says.");
    expect(refusalReasonLabel("brand_new")).not.toContain("brand_new");
  });

  it("labels exactly the codes the server sends, and no code it never sends", () => {
    // backend/app/modules/agent_push/preflight.py REFUSAL_REASONS, the
    // host-key bound's two (host_key.confirmed_key_for), and -- since review
    // Minor 2 -- dispatch.DISPATCH_REFUSAL_REASONS, each recorded as
    // push.refused. Nothing on the server emits a "WinRM unreachable" code: a
    // label for a code that never arrives is a promise about a check the
    // server does not make.
    expect(Object.keys(REFUSAL_REASON_LABELS).sort()).toEqual(
      [
        "credential_not_scoped",
        "host_key_changed",
        "host_key_unconfirmed",
        "no_asset",
        "ambiguous_asset",
        "no_authorization",
        "no_site_server",
        "no_winrm_site_server",
        "site_server_too_old",
        "no_dns_name",
        "invalid_dns_name",
        "wrong_credential_kind",
        "no_console_url",
        "outside_realm",
        "realm_not_recorded",
        "realm_changed",
      ].sort(),
    );
  });
});
